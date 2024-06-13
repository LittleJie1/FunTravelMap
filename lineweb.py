from flask import Flask, request, jsonify, abort
from flask_cors import CORS
import json
from time import strftime
from pymongo.mongo_client import MongoClient
import requests
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
CORS(app, resources={r"/*": {"origins": "*"}})
with open('env.json') as f:
    env = json.load(f)
configuration = Configuration(access_token=env['CHANNEL_ACCESS_TOKEN'])
handler = WebhookHandler(env['CHANNEL_SECRET'])

uri = "mongodb+srv://jiejieupup:1qaz2wsx@funtravelmap.nw4tnce.mongodb.net/?retryWrites=true&w=majority&appName=funtravelmap&tls=true&tlsAllowInvalidCertificates=true"
mongo_client = MongoClient(uri)
try:
    mongo_client.admin.command('ping')
    print("Pinged your deployment. You successfully connected to MongoDB!")
except Exception as e:
    print(e)
db = mongo_client['web']
users = db['travel']

GOOGLE_MAPS_API_KEY = env['GOOGLE_MAPS_API_KEY']

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

@handler.add(FollowEvent)
def handle_follow(event):
    userid = event.source.user_id
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        profile = line_bot_api.get_profile(userid)
        existing_user = users.find_one({"_id": userid})

        if not existing_user:
            # insert into MongoDB
            u = {
                "_id": userid,
                "display_name": profile.display_name,
                "user_id": profile.user_id,
                "picture_url": profile.picture_url,
                "status_message": profile.status_message,
                "language": profile.language,
                "follow": strftime('%Y/%m/%d-%H:%M:%S'),
                "unfollow": None,
                "itineraries": []
            }
            users.insert_one(u)
        else:
            users.update_one(
                {"_id": userid},
                {"$set": {"follow": strftime('%Y/%m/%d-%H:%M:%S'), "unfollow": None}}
            )

@handler.add(UnfollowEvent)
def handle_unfollow(event):
    userid = event.source.user_id
    users.update_one(
        {"_id": userid},
        {"$set": {"unfollow": strftime('%Y/%m/%d-%H:%M:%S')}}
    )

@app.route('/get_itineraries', methods=['GET']) #讀取現有行程
def get_itineraries():
    user_id = request.args.get('user_id')
    
    # 查找指定用戶的資料
    user = users.find_one({"_id": user_id})
    if not user:
        return jsonify({'status': 'error', 'message': 'User not found'}), 404

    # 獲取用戶的行程列表
    itineraries = user.get('itineraries', [])
    return jsonify({'status': 'success', 'itineraries': itineraries})

@app.route('/add_itinerary', methods=['POST']) #新建行程
def add_itinerary():
    data = request.json
    user_id = data.get('user_id')
    itinerary = data.get('itinerary')

    # 打印收到的数据以便调试
    print(f"Received itinerary: {itinerary} for user: {user_id}")

    # 更新用戶的行程列表，添加新的行程
    result = users.update_one(
        {"_id": user_id},
        {"$push": {"itineraries": itinerary}}
    )

    # 檢查更新操作是否成功
    if result.modified_count > 0:
        return jsonify({'status': 'success'})
    else:
        return jsonify({'status': 'error', 'message': 'Failed to add itinerary'}), 500

@app.route('/route/<user_id>/<itinerary_id>', methods=['GET'])  #最佳路線規劃
def route(user_id, itinerary_id):
    # 從 MongoDB 中獲取用戶資料
    user = users.find_one({"_id": user_id})
    if not user:
        return jsonify({'error': 'User not found'}), 404
    
    # 從用戶資料中獲取指定的行程
    itinerary = next((it for it in user['itineraries'] if it['itinerary_id'] == itinerary_id), None)
    if not itinerary:
        return jsonify({'error': 'Itinerary not found'}), 404

    # 獲取行程中的景點列表
    places = itinerary['places']
    if len(places) < 2:
        return jsonify({'error': 'At least two places are required.'}), 400

    # 起點為景點列表中的第一個
    origin = places[0]
    # 終點為景點列表中的最後一個
    destination = places[-1]
    # 途經點為介於起點和終點之間的景點
    waypoints = places[1:-1]

    # 構建 Google Maps Directions API 請求
    url = 'https://maps.googleapis.com/maps/api/directions/json'
    params = {
        'origin': f'{origin["latitude"]},{origin["longitude"]}',  # 起點經緯度
        'destination': f'{destination["latitude"]},{destination["longitude"]}',  # 終點經緯度
        'waypoints': '|'.join([f'{wp["latitude"]},{wp["longitude"]}' for wp in waypoints]),  # 途經點經緯度
        'key': GOOGLE_MAPS_API_KEY,  # Google Maps API 金鑰
        'optimizeWaypoints': 'true'  # 優化途經點順序
    }

    # 發送請求到 Google Maps Directions API
    response = requests.get(url, params=params)
    if response.status_code != 200:
        return jsonify({'error': 'Failed to get directions from Google Maps API.'}), 500

    # 解析 API 返回的結果
    directions = response.json()
    if directions['status'] != 'OK':
        return jsonify({'error': 'No route found.'}), 404

    # 提取最佳路線順序
    waypoint_order = directions['routes'][0]['waypoint_order']
    
    # 根據返回的順序重排景點列表
    optimized_places = [origin] + [waypoints[i] for i in waypoint_order] + [destination]
    
    # 更新 MongoDB 中的行程資料
    users.update_one(
        {"_id": user_id, "itineraries.itinerary_id": itinerary_id},
        {"$set": {"itineraries.$.places": optimized_places}}
    )

    # 返回最佳路線信息
    return jsonify(directions['routes'][0])

if __name__ == '__main__':
    app.run()
