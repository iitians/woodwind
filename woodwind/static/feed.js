$(function(){

    function clickOlderLink(evt) {
        evt.preventDefault();
        $.get(this.href, function(result) {
            $(".pager").replaceWith(
                $("article,.pager", $(result)));
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

        var form = $(this).closest('form');
        var replyArea = form.parent();
        var endpoint = form.attr('action');
        var responseArea = $('.micropub-response', replyArea);
        var formData = form.serializeArray();
        formData.push({name: this.name, value: this.value});

        $.post(
            form.attr('action'),
            formData,
            function(result) {
                if (Math.floor(result.code / 100) == 2) {
                    responseArea.html('<a target="_blank" href="' + result.location + '">Success!</a>');
                    $(".micropub-form textarea").val("");
                } else {
                    responseArea.html('Failure');
                }
            },
            'json'
        );


        responseArea.html('Posting…');
    }

    function attachListeners() {
        $(".reply-form, .like-form").css('display', 'none');

        $("#older-link").off('click').click(clickOlderLink);
        $(".show-reply-form").off('click').click(clickShowReplyForm);
        $(".show-like-form").off('click').click(clickShowLikeForm);

        $(".micropub-form button[type='submit']").off('click').click(submitMicropubForm);

        $(".micropub-form .content").focus(function (){
            $(this).animate({ height: "4em" }, 200);
        });
    }


    // topic will be user:id or feed:id
    function webSocketSubscribe(topic) {
        if ('WebSocket' in window) {
            var ws = new WebSocket(window.location.origin
                                   .replace(/https?:\/\//, 'ws://')
                                   .replace(/(:\d+)?$/, ':8077'));
            ws.onopen = function(event) {
                // send the topic
                console.log('subscribing to topic: ' + topic);
                ws.send(topic);
            };
            ws.onmessage = function(event) {
                var data = JSON.parse(event.data);
                data.entries.forEach(function(entryHtml) {
                    $('body main').prepend(entryHtml);
                });
                attachListeners();
            };
        }
    }

    attachListeners();
    if (WS_TOPIC) {
        webSocketSubscribe(WS_TOPIC);
    }
});
