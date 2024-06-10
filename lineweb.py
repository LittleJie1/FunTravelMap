from flask import Flask, request, abort
import json
from time import strftime
from pymongo.mongo_client import MongoClient

from linebot.v3 import (
    WebhookHandler
)
from linebot.v3.exceptions import (
    InvalidSignatureError
)
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    ReplyMessageRequest,
    TextMessage
)
from linebot.v3.webhooks import (
    MessageEvent,
    TextMessageContent,
    FollowEvent,
    UnfollowEvent,
)

app = Flask(__name__)

with open('env.json') as f:
    env = json.load(f)
configuration = Configuration(access_token=env['CHANNEL_ACCESS_TOKEN'])
handler = WebhookHandler(env['CHANNEL_SECRET'])

# uri = "mongodb+srv://jiejieupup:<password>@funtravelmap.nw4tnce.mongodb.net/?retryWrites=true&w=majority&appName=funtravelmap&tls=true&tlsAllowInvalidCertificates=true"
# mongo_client = MongoClient(uri)

# try:
#     mongo_client.admin.command('ping')
#     print("Pinged your deployment. You successfully connected to MongoDB!")
# except Exception as e:
#     print(e)

# db = mongo_client['thisisjie']
# users = db['users']

@app.route("/callback", methods=['POST'])
def callback():
    # get X-Line-Signature header value
    signature = request.headers['X-Line-Signature']

    # get request body as text
    body = request.get_data(as_text=True)
    app.logger.info("Request body: " + body)

    # handle webhook body
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        app.logger.info("Invalid signature. Please check your channel access token/channel secret.")
        abort(400)

    return 'OK'


@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.reply_message_with_http_info(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=event.message.text)]
            )
        )
# @handler.add(FollowEvent)
# def handle_message(event):
#     userid = event.source.user_id
#     with ApiClient(configuration) as api_client:
#         line_bot_api = MessagingApi(api_client)
#         profile = line_bot_api.get_profile(userid)

#         # insert into MongoDB
#         u = dict(profile)
#         u['_id'] = userid
#         u['follow'] = strftime('%Y/%m/%d-%H:%M:%S')
#         u['unfollow'] = None
#         try: 
#             users.insert_one(u)
#         except Exception as e:
#             print(e)  

# @handler.add(UnfollowEvent)
# def handle_message(event):
#     userid = event.source.user_id
#     # system behavior 

if __name__ == "__main__":
    app.run()