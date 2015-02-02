from . import tasks
from .extensions import db, login_mgr, micropub
from .models import Feed, Entry, User
import flask.ext.login as flask_login
import bs4
import feedparser
import flask
import mf2py
import mf2util
import requests
import urllib

views = flask.Blueprint('views', __name__)


@views.route('/')
def index():
    page = int(flask.request.args.get('page', 1))
    entries = []
    if flask_login.current_user.is_authenticated():
        per_page = flask.current_app.config.get('PER_PAGE', 30)
        offset = (page - 1) * per_page
        feed_ids = set(f.id for f in flask_login.current_user.feeds)
        if feed_ids:
            entries = Entry.query.filter(Entry.feed_id.in_(feed_ids))\
                                 .order_by(Entry.published.desc())\
                                 .offset(offset).limit(per_page).all()
    return flask.render_template('feed.jinja2', entries=entries, page=page)


@views.route('/install')
def install():
    db.create_all()
    return 'Success!'


@views.route('/feeds')
def feeds():
    feeds = flask_login.current_user.feeds
    return flask.render_template('feeds.jinja2', feeds=feeds)


@views.route('/update_feed')
def update_feed():
    feed_id = flask.request.args.get('id')
    tasks.update_feed.delay(feed_id)
    return flask.redirect(flask.url_for('.feeds'))


@views.route('/delete_feed', methods=['POST'])
def delete_feed():
    feed_id = flask.request.form.get('id')
    feed = Feed.query.get(feed_id)
    db.session.delete(feed)
    db.session.commit()
    flask.flash('Deleted {} ({})'.format(feed.name, feed.feed))
    return flask.redirect(flask.url_for('.feeds'))


@views.route('/edit_feed', methods=['POST'])
def edit_feed():
    feed_id = flask.request.form.get('id')
    feed_name = flask.request.form.get('name')
    feed_url = flask.request.form.get('feed')

    feed = Feed.query.get(feed_id)
    if feed_name:
        feed.name = feed_name
    if feed_url:
        feed.feed = feed_url

    db.session.commit()
    flask.flash('Edited {} ({})'.format(feed.name, feed.feed))
    return flask.redirect(flask.url_for('.feeds'))


@views.route('/login')
def login():
    me = flask.request.args.get('me')
    if me:
        return micropub.authorize(
            me, flask.url_for('.login_callback', _external=True),
            next_url=flask.request.args.get('next'),
            scope='write')
    return flask.render_template('login.jinja2')


@views.route('/login-callback')
@micropub.authorized_handler
def login_callback(resp):
    if not resp.me:
        flask.flash('Login error: ' + resp.error)
        return flask.redirect(flask.url_for('.login'))

    if resp.error:
        flask.flash('Warning: ' + resp.error)

    domain = urllib.parse.urlparse(resp.me).netloc
    user = load_user(domain)
    if not user:
        user = User()
        user.domain = domain
        db.session.add(user)

    user.micropub_endpoint = resp.micropub_endpoint
    user.access_token = resp.access_token
    db.session.commit()

    flask_login.login_user(user, remember=True)
    return flask.redirect(resp.next_url or flask.url_for('.index'))


@login_mgr.user_loader
def load_user(domain):
    return User.query.filter_by(domain=domain).first()


@views.route('/subscribe', methods=['GET', 'POST'])
def subscribe():
    if flask.request.method == 'POST':
        origin = flask.request.form.get('origin')
        if origin:
            type = None
            feed = None
            typed_feed = flask.request.form.get('feed')
            if typed_feed:
                type, feed = typed_feed.split('|', 1)
            else:
                feeds = find_possible_feeds(origin)
                if not feeds:
                    flask.flash('No feeds found for: ' + origin)
                    return flask.redirect(flask.url_for('.subscribe'))
                if len(feeds) > 1:
                    return flask.render_template(
                        'select-feed.jinja2', origin=origin, feeds=feeds)
                feed = feeds[0]['feed']
                type = feeds[0]['type']
            new_feed = add_subscription(origin, feed, type)
            flask.flash('Successfully subscribed to: {}'.format(new_feed.name))
            return flask.redirect(flask.url_for('.index'))
        else:
            flask.abort(400)

    return flask.render_template('subscribe.jinja2')


def add_subscription(origin, feed_url, type):
    feed = Feed.query.filter_by(feed=feed_url, type=type).first()
    if not feed:
        if type == 'html':
            flask.current_app.logger.debug('mf2py parsing %s', feed_url)
            parsed = mf2util.interpret_feed(mf2py.parse(url=feed_url), feed_url)
            name = parsed.get('name')
            if not name or len(name) > 140:
                p = urllib.parse.urlparse(origin)
                name = p.netloc + p.path
            feed = Feed(name=name, origin=origin, feed=feed_url, type=type)
        elif type == 'xml':
            flask.current_app.logger.debug('feedparser parsing %s', feed_url)
            parsed = feedparser.parse(feed_url)
            feed = Feed(name=parsed.feed and parsed.feed.title,
                        origin=origin, feed=feed_url, type=type)
    if feed:
        db.session.add(feed)
        flask_login.current_user.feeds.append(feed)
        db.session.commit()
        # go ahead and update the fed
        tasks.update_feed.delay(feed.id)
    return feed


def find_possible_feeds(origin):
    # scrape an origin source to find possible alternative feeds
    resp = requests.get(origin)

    feeds = []

    xml_feed_types = [
        'application/rss+xml',
        'application/atom+xml',
        'application/rdf+xml',
        'application/xml',
    ]
    xml_mime_types = xml_feed_types + [
        'text/xml',
        'text/rss+xml',
        'text/atom+xml',
    ]

    content_type = resp.headers['content-type']
    content_type = content_type.split(';', 1)[0].strip()
    if content_type in xml_mime_types:
        feeds.append({
            'origin': origin,
            'feed': origin,
            'type': 'xml',
        })

    elif content_type == 'text/html':
        # if text/html, then parse and look for rel="alternate"
        soup = bs4.BeautifulSoup(resp.text)
        for link in soup.find_all('link', {'rel': 'alternate'}):
            if link.get('type') in xml_feed_types:
                feed_url = urllib.parse.urljoin(origin, link.get('href'))
                feeds.append({
                    'origin': origin,
                    'feed': feed_url,
                    'type': 'xml',
                })

        hfeed = mf2util.interpret_feed(mf2py.parse(doc=resp.text), origin)
        if hfeed.get('entries'):
            feeds.append({
                'origin': origin,
                'feed': origin,
                'type': 'html',
            })

    return feeds
