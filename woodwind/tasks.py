from contextlib import contextmanager
from flask import current_app
from redis import StrictRedis
from woodwind import util
from woodwind.extensions import db
from woodwind.models import Feed, Entry
import bs4
import datetime
import feedparser
import itertools
import json
import mf2py
import mf2util
import re
import requests
import rq
import sys
import time
import urllib.parse

# normal update interval for polling feeds
UPDATE_INTERVAL = datetime.timedelta(hours=1)
# update interval when polling feeds that are push verified
UPDATE_INTERVAL_PUSH = datetime.timedelta(days=1)

TWITTER_RE = re.compile(
    r'https?://(?:www\.|mobile\.)?twitter\.com/(\w+)/status(?:es)?/(\w+)')
TAG_RE = re.compile(r'</?\w+[^>]*?>')
COMMENT_RE = re.compile(r'<!--[^>]*?-->')
JAM_RE = re.compile(
    '\s*\u266b (?:https?://)?[a-z0-9._\-]+\.[a-z]{2,9}(?:/\S*)?')

AUDIO_ENCLOSURE_TMPL = '<p><audio class="u-audio" src="{href}" controls '\
                       'preload=none ><a href="{href}">audio</a></audio></p>'
VIDEO_ENCLOSURE_TMPL = '<p><video class="u-video" src="{href}" controls '\
                       'preload=none ><a href="{href}">video</a></video></p>'

redis = StrictRedis()
q_high = rq.Queue('high', connection=redis)
q = rq.Queue('low', connection=redis)


_app = None


@contextmanager
def flask_app():
    global _app
    if _app is None:
        from woodwind import create_app
        _app = create_app()
    with _app.app_context():
        try:
            yield _app
        except:
            _app.logger.exception('Unhandled exception')


def tick():
    """Checks all feeds to see if any of them are ready for an update.
    Makes use of uWSGI timers to run every 5 minutes, without needing
    a separate process to fire ticks.
    """
    def should_update(feed, now):
        if not feed.last_checked:
            return True

        if not feed.subscriptions:
            return False

        if feed.failure_count > 8:
            update_interval = datetime.timedelta(days=1)
        elif feed.failure_count > 4:
            update_interval = datetime.timedelta(hours=8)
        elif feed.failure_count > 2:
            update_interval = datetime.timedelta(hours=4)
        else:
            update_interval = UPDATE_INTERVAL

        # PuSH feeds don't need to poll very frequently
        if feed.push_verified:
            update_interval = max(update_interval, UPDATE_INTERVAL_PUSH)

        return now - feed.last_checked > update_interval

    with flask_app():
        now = datetime.datetime.utcnow()
        current_app.logger.info('Tick {}'.format(now))
        for feed in Feed.query.all():
            current_app.logger.debug(
                'Feed %s last checked %s', feed, feed.last_checked)
            if should_update(feed, now):
                q.enqueue(update_feed, feed.id)


def update_feed(feed_id, content=None,
                content_type=None, is_polling=True):

    def is_expected_content_type(feed_type):
        if not content_type:
            return True
        if feed_type == 'html':
            return content_type == 'text/html'
        if feed_type == 'xml':
            return content_type in [
                'application/rss+xml',
                'application/atom+xml',
                'application/rdf+xml',
                'application/xml',
                'text/xml',
            ]

    with flask_app() as app:
        feed = Feed.query.get(feed_id)
        current_app.logger.info('Updating {}'.format(feed))

        now = datetime.datetime.utcnow()

        new_ids = []
        updated_ids = []
        reply_pairs = []

        try:
            if content and is_expected_content_type(feed.type):
                current_app.logger.info('using provided content. size=%d',
                                        len(content))
            else:
                current_app.logger.info('fetching feed: %s', feed)

                try:
                    response = util.requests_get(feed.feed)
                except:
                    feed.last_response = 'exception while retrieving: {}'.format(
                        sys.exc_info()[0])
                    feed.failure_count += 1
                    return

                if response.status_code // 100 != 2:
                    current_app.logger.warn(
                        'bad response from %s. %r: %r', feed.feed, response,
                        response.text)
                    feed.last_response = 'bad response while retrieving: {}: {}'.format(
                        response, response.text)
                    feed.failure_count += 1
                    return

                feed.failure_count = 0
                feed.last_response = 'success: {}'.format(response)

                if is_polling:
                    check_push_subscription(feed, response)
                content = get_response_content(response)

            # backfill if this is the first pull
            backfill = len(feed.entries) == 0
            if feed.type == 'xml':
                result = process_xml_feed_for_new_entries(
                    feed, content, backfill, now)
            elif feed.type == 'html':
                result = process_html_feed_for_new_entries(
                    feed, content, backfill, now)
            else:
                result = []

            for entry in result:
                old = Entry.query\
                           .filter(Entry.feed == feed)\
                           .filter(Entry.uid == entry.uid)\
                           .order_by(Entry.id.desc())\
                           .first()
                # have we seen this post before
                if not old:
                    # set a default value for published if none is provided
                    entry.published = entry.published or now
                    in_reply_tos = entry.get_property('in-reply-to', [])
                    feed.entries.append(entry)
                    db.session.commit()

                    new_ids.append(entry.id)
                    for irt in in_reply_tos:
                        reply_pairs.append((entry.id, irt))

                elif not is_content_equal(old, entry):
                    entry.published = entry.published or old.published
                    in_reply_tos = entry.get_property('in-reply-to', [])
                    # we're updating an old entriy, use the original
                    # retrieved time
                    entry.retrieved = old.retrieved
                    feed.entries.remove(old)
                    # punt on deleting for now, learn about cascade
                    # and stuff later
                    # session.delete(old)
                    feed.entries.append(entry)
                    db.session.commit()

                    updated_ids.append(entry.id)
                    for irt in in_reply_tos:
                        reply_pairs.append((entry.id, irt))

                else:
                    current_app.logger.debug(
                        'skipping previously seen post %s', old.permalink)

            for entry_id, in_reply_to in reply_pairs:
                fetch_reply_context(entry_id, in_reply_to, now)

        finally:
            if is_polling:
                feed.last_checked = now
            if new_ids or updated_ids:
                feed.last_updated = now
            db.session.commit()
            if new_ids:
                notify_feed_updated(app, feed_id, new_ids)


def check_push_subscription(feed, response):
    def build_callback_url():
        return '{}://{}/_notify/{}'.format(
            getattr(current_app.config, 'PREFERRED_URL_SCHEME', 'http'),
            current_app.config['SERVER_NAME'],
            feed.id)

    def send_request(mode, hub, topic):
        hub = urllib.parse.urljoin(feed.feed, hub)
        topic = urllib.parse.urljoin(feed.feed, topic)
        current_app.logger.debug(
            'sending %s request for hub=%r, topic=%r', mode, hub, topic)
        r = requests.post(hub, data={
            'hub.mode': mode,
            'hub.topic': topic,
            'hub.callback': build_callback_url(),
            'hub.secret': feed.get_or_create_push_secret(),
            'hub.verify': 'sync',  # backcompat with 0.3
        })
        current_app.logger.debug('%s response %r', mode, r)

    expiry = feed.push_expiry
    old_hub = feed.push_hub
    old_topic = feed.push_topic
    hub = response.links.get('hub', {}).get('url')
    topic = response.links.get('self', {}).get('url')

    current_app.logger.debug('link headers. links=%s, hub=%s, topic=%s',
                             response.links, hub, topic)
    if not hub or not topic:
        # try to find link rel elements
        if feed.type == 'html':
            soup = bs4.BeautifulSoup(get_response_content(response))
            if not hub:
                hub_link = soup.find('link', rel='hub')
                hub = hub_link and hub_link.get('href')
            if not topic:
                self_link = soup.find('link', rel='self')
                topic = self_link and self_link.get('href')
        elif feed.type == 'xml':
            parsed = feedparser.parse(get_response_content(response))
            links = parsed.feed.get('links')
            if links:
                if not hub:
                    hub = next((link['href'] for link in links
                                if 'hub' in link['rel']), None)
                if not topic:
                    topic = next((link['href'] for link in links
                                  if 'self' in link['rel']), None)

    if ((expiry and expiry - datetime.datetime.utcnow()
            <= UPDATE_INTERVAL_PUSH)
            or hub != old_hub or topic != old_topic or not feed.push_verified):
        feed.push_hub = hub
        feed.push_topic = topic
        feed.push_verified = False
        feed.push_expiry = None
        db.session.commit()

        if old_hub and old_topic and hub != old_hub and topic != old_topic:
            send_request('unsubscribe', old_hub, old_topic)

        if hub and topic:
            send_request('subscribe', hub, topic)

        db.session.commit()


def notify_feed_updated(app, feed_id, entry_ids):
    """Render the new entries and publish them to redis
    """
    from flask import render_template
    import flask.ext.login as flask_login
    current_app.logger.debug(
        'notifying feed updated for entries %r', entry_ids)

    feed = Feed.query.get(feed_id)
    entries = Entry.query\
                   .filter(Entry.id.in_(entry_ids))\
                   .order_by(Entry.retrieved.desc(),
                             Entry.published.desc())\
                   .all()

    for s in feed.subscriptions:
        with app.test_request_context():
            flask_login.login_user(s.user, remember=True)
            rendered = []
            for e in entries:
                e.subscription = s
                rendered.append(render_template('_entry.jinja2', entry=e))

            message = json.dumps({
                'user': s.user.id,
                'feed': feed.id,
                'subscription': s.id,
                'entries': rendered,
            })

            topics = []
            if not s.exclude:
                topics.append('user:{}'.format(s.user.id))
            topics.append('subsc:{}'.format(s.id))

            for topic in topics:
                redis.publish('woodwind_notify:{}'.format(topic), message)


def is_content_equal(e1, e2):
    """The criteria for determining if an entry that we've seen before
    has been updated. If any of these fields have changed, we'll scrub the
    old entry and replace it with the updated one.
    """
    def normalize(content):
        """Strip HTML tags, added to prevent a specific case where Wordpress
        syntax highlighting (crayon) generates slightly different
        markup every time it's called.
        """
        if content:
            content = TAG_RE.sub('', content)
            content = COMMENT_RE.sub('', content)
        return content

    return (e1.title == e2.title
            and normalize(e1.content) == normalize(e2.content)
            and e1.author_name == e2.author_name
            and e1.author_url == e2.author_url
            and e1.author_photo == e2.author_photo
            and e1.properties == e2.properties)


def process_xml_feed_for_new_entries(feed, content, backfill, now):
    current_app.logger.debug('fetching xml feed: %s', feed)
    parsed = feedparser.parse(content, response_headers={
        'content-location': feed.feed,
    })
    feed_props = parsed.get('feed', {})
    default_author_url = feed_props.get('author_detail', {}).get('href')
    default_author_name = feed_props.get('author_detail', {}).get('name')
    default_author_photo = feed_props.get('logo')

    current_app.logger.debug('found {} entries'.format(len(parsed.entries)))

    # work from the bottom up (oldest first, usually)
    for p_entry in reversed(parsed.entries):
        current_app.logger.debug('processing entry {}'.format(
            str(p_entry)[:256]))
        permalink = p_entry.get('link')
        uid = p_entry.get('id') or permalink

        if not uid:
            continue

        if 'updated_parsed' in p_entry and p_entry.updated_parsed:
            updated = datetime.datetime.fromtimestamp(
                time.mktime(p_entry.updated_parsed))
        else:
            updated = None

        if 'published_parsed' in p_entry and p_entry.published_parsed:
            published = datetime.datetime.fromtimestamp(
                time.mktime(p_entry.published_parsed))
        else:
            published = updated

        retrieved = now
        if backfill and published:
            retrieved = published

        title = p_entry.get('title')

        content = None
        content_list = p_entry.get('content')
        if content_list:
            content = content_list[0].value
        else:
            content = p_entry.get('summary')

        if title and content:
            title_trimmed = title.rstrip('...').rstrip('…')
            if content.startswith(title_trimmed):
                title = None

        for link in p_entry.get('links', []):
            if link.type == 'audio/mpeg' or link.type == 'audio/mp3':
                audio = AUDIO_ENCLOSURE_TMPL.format(href=link.get('href'))
                content = (content or '') + audio
            if (link.type == 'video/x-m4v'
                    or link.type == 'video/x-mp4'
                    or link.type == 'video/mp4'):
                video = VIDEO_ENCLOSURE_TMPL.format(href=link.get('href'))
                content = (content or '') + video

        entry = Entry(
            published=published,
            updated=updated,
            uid=uid,
            permalink=permalink,
            retrieved=retrieved,
            title=p_entry.get('title'),
            content=content,
            content_cleaned=util.clean(content),
            author_name=p_entry.get('author_detail', {}).get('name')
            or default_author_name,
            author_url=p_entry.get('author_detail', {}).get('href')
            or default_author_url,
            author_photo=default_author_photo
            or fallback_photo(feed.origin))

        yield entry


def process_html_feed_for_new_entries(feed, content, backfill, now):
    # strip noscript tags before parsing, since we definitely aren't
    # going to preserve js
    content = re.sub('</?noscript[^>]*>', '', content, flags=re.IGNORECASE)
    parsed = mf2util.interpret_feed(
        mf2py.parse(url=feed.feed, doc=content), feed.feed)
    hfeed = parsed.get('entries', [])

    for hentry in hfeed:
        current_app.logger.debug('building entry: %s', hentry.get('url'))
        entry = hentry_to_entry(hentry, feed, backfill, now)
        if entry:
            current_app.logger.debug('built entry: %s', entry.permalink)
            yield entry


def hentry_to_entry(hentry, feed, backfill, now):
    permalink = url = hentry.get('url')
    uid = hentry.get('uid') or url
    if not uid:
        return

    # hentry = mf2util.interpret(mf2py.Parser(url=url).to_dict(), url)
    # permalink = hentry.get('url') or url
    # uid = hentry.get('uid') or uid

    # TODO repost = next(iter(hentry.get('repost-of', [])), None)

    title = hentry.get('name')
    content = hentry.get('content')
    if not content:
        content = title
        title = None

    published = hentry.get('published')
    updated = hentry.get('updated')

    if published:
        # make sure published is in UTC and strip the timezone
        if hasattr(published, 'tzinfo') and published.tzinfo:
            published = published.astimezone(datetime.timezone.utc)\
                                 .replace(tzinfo=None)
        # convert datetime.date to datetime.datetime
        elif not hasattr(published, 'hour'):
            published = datetime.datetime(
                year=published.year,
                month=published.month,
                day=published.day)

    # retrieved time is now unless we're backfilling old posts
    retrieved = now
    if backfill and published and published < retrieved:
        retrieved = published

    author = hentry.get('author', {})
    author_name = author.get('name')
    author_photo = author.get('photo')
    author_url = author.get('url')

    entry = Entry(
        uid=uid,
        retrieved=retrieved,
        permalink=permalink,
        published=published,
        updated=updated,
        title=title,
        content=content,
        content_cleaned=util.clean(content),
        author_name=author_name,
        author_photo=author_photo or (feed and fallback_photo(feed.origin)),
        author_url=author_url)

    # complex properties, convert from list of complex objects to a
    # list of URLs
    for prop in ('in-reply-to', 'like-of', 'repost-of'):
        values = hentry.get(prop)
        if values:
            entry.set_property(prop, [value['url'] for value in values
                                      if 'url' in value])

    # simple properties, just transfer them over wholesale
    for prop in ('syndication', 'location'):
        value = hentry.get(prop)
        if value:
            entry.set_property(prop, value)

    # does it look like a jam?
    plain = hentry.get('content-plain')
    if plain and JAM_RE.match(plain):
        entry.set_property('jam', True)

    current_app.logger.debug('entry properties %s', entry.properties)
    return entry


def fetch_reply_context(entry_id, in_reply_to, now):
    with flask_app():
        entry = Entry.query.get(entry_id)
        context = Entry.query\
                       .join(Entry.feed)\
                       .filter(Entry.permalink==in_reply_to, Feed.type == 'html')\
                       .first()

        if not context:
            current_app.logger.info('fetching in-reply-to url: %s',
                                    in_reply_to)
            parsed = mf2util.interpret(
                mf2py.parse(url=proxy_url(in_reply_to)), in_reply_to)
            if parsed:
                context = hentry_to_entry(parsed, None, False, now)

        if context:
            entry.reply_context.append(context)
            db.session.commit()


def proxy_url(url):
    if ('TWITTER_AU_KEY' in current_app.config
            and 'TWITTER_AU_SECRET' in current_app.config):
        # swap out the a-u url for twitter urls
        match = TWITTER_RE.match(url)
        if match:
            proxy_url = (
                'https://twitter-activitystreams.appspot.com/@me/@all/@app/{}?'
                .format(match.group(2)) + urllib.parse.urlencode({
                    'format': 'html',
                    'access_token_key':
                    current_app.config['TWITTER_AU_KEY'],
                    'access_token_secret':
                    current_app.config['TWITTER_AU_SECRET'],
                }))
            current_app.logger.debug('proxied twitter url %s', proxy_url)
            return proxy_url
    return url


def fallback_photo(url):
    """Use favatar to find an appropriate photo for any URL"""
    domain = urllib.parse.urlparse(url).netloc
    return 'http://www.google.com/s2/favicons?domain=' + domain


def get_response_content(response):
    # if no charset is provided in the headers, figure out the
    # encoding from the content
    if 'charset' not in response.headers.get('content-type', ''):
        encodings = requests.utils.get_encodings_from_content(response.text)
        if encodings:
            response.encoding = encodings[0]
    return response.text
