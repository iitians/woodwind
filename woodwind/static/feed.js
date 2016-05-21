$(function(){

    function updateTimestamps() {
        $(".permalink time").each(function() {
            var absolute = $(this).attr('datetime');
            var formatted = moment.utc(absolute).fromNow();
            $(this).text(formatted);
        })
    }

    function clickOlderLink(evt) {
        evt.preventDefault();
        $.get(this.href, function(result) {
            var $newElements = $("article,.pager", $(result));
            $(".pager").replaceWith($newElements);
            $newElements.each(function () {
                twttr.widgets.load(this);
            });
            attachListeners();
        });
    }

    function clickShowReplyForm(evt) {
        var a = $(this);
        evt.preventDefault();
        $(".like-form", a.parent()).hide();
        $(".reply-form", a.parent()).toggle();//css('display', 'inherit');
        //a.css('display', 'none');
    }

    function clickShowLikeForm(evt) {
        var a = $(this);
        evt.preventDefault();
        $(".reply-form", a.parent()).hide();
        $(".like-form", a.parent()).toggle();
        //a.css('display', 'none');
    }

    function submitMicropubForm(evt) {
        evt.preventDefault();

        var button = this;
        var form = $(button).closest('form');
        var replyArea = form.parent();
        var endpoint = form.attr('action');
        var responseArea = $('.micropub-response', replyArea);
        var formData = form.serializeArray();
        formData.push({name: button.name, value: button.value});

        $.post(
            endpoint,
            formData,
            function(result) {
                if (Math.floor(result.code / 100) == 2) {
                    responseArea.html('<a target="_blank" href="' + result.location + '">Success!</a>');
                    $("textarea", form).val("");

                    if (button.value === 'rsvp-yes') {
                        $(".rsvps", form).html('✓ Going');
                    } else if (button.value === 'rsvp-maybe') {
                        $(".rsvps", form).html('? Interested');
                    } else if (button.value === 'rsvp-no') {
                        $(".rsvps", form).html('✗ Not Going');
                    }

                } else {
                    responseArea.html('Failure');
                }
            },
            'json'
        );


        responseArea.html('Posting…');
    }

    function attachListeners() {
        $("#older-link").off('click').click(clickOlderLink);
        $(".micropub-form button[type='submit']").off('click').click(submitMicropubForm);

        // Post by ctrl/cmd + enter in the text area
        $(".micropub-form textarea.content").keyup(function(e) {
            if ((e.ctrlKey || e.metaKey) && (e.keyCode == 13 || e.keyCode == 10)) {
                var button = $(e.target).closest('form').find('button[value=reply]');
                button[0].click();
            }
        });

        $(".micropub-form .content").focus(function () {
            $(this).animate({ height: "4em" }, 200);
            var $target = $(evt.target);
        });
    }


    function clickUnfoldLink(evt) {
        $('#fold').after($('#fold').children())
        $('#unfold-link').hide();
    }


    function foldNewEntries(entries) {
        $('#fold').prepend(entries.join('\n'));
        attachListeners();
        $('#unfold-link').text($('#fold>article:not(.reply-context)').length + " New Posts");
        $('#unfold-link').off('click').click(clickUnfoldLink);
        $('#unfold-link').show();

        // load twitter embeds
        twttr.widgets.load($('#fold').get(0));
    }

    // topic will be user:id or feed:id
    function webSocketSubscribe(topic) {
        if ('WebSocket' in window) {
            var ws = new WebSocket(window.location.origin
                                   .replace(/http:\/\//, 'ws://')
                                   .replace(/https:\/\//, 'wss://')
                                   + '/_updates');

            ws.onopen = function(event) {
                // send the topic
                console.log('subscribing to topic: ' + topic);
                ws.send(topic);
            };
            ws.onmessage = function(event) {
                var data = JSON.parse(event.data);
                foldNewEntries(data.entries);
            };
        }
    }

    attachListeners();

    $(document).on("keypress", function(e) {
        if (e.which === 46) {
            clickUnfoldLink();
        }
    });

    if (WS_TOPIC) {
        webSocketSubscribe(WS_TOPIC);
    }

    updateTimestamps();
    window.setInterval(updateTimestamps, 60 * 1000);

});
