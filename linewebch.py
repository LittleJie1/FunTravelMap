from flask import Flask, request, jsonify, abort
from flask_cors import CORS
import json
from time import strftime
from pymongo.mongo_client import MongoClient
import requests
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    ReplyMessageRequest,
    TextMessage
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent, FollowEvent, UnfollowEvent
from datetime import datetime
import pytz

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

# Load environment variables from env.json
with open('env.json') as f:
    env = json.load(f)

# Line Messaging API configuration
configuration = Configuration(access_token=env['CHANNEL_ACCESS_TOKEN'])
handler = WebhookHandler(env['CHANNEL_SECRET'])

# MongoDB Atlas connection
uri = env["MONGODB_URI"]
mongo_client = MongoClient(uri)

try:
    mongo_client.admin.command('ping')
    print("Pinged your deployment. You successfully connected to MongoDB!")
except Exception as e:
    print("Error connecting to MongoDB:", e)

db2 = mongo_client['funtravelmap']
checkins_collection = db2['checkins']
db = mongo_client['web']
users = db['travel']
GOOGLE_MAPS_API_KEY = env['GOOGLE_MAPS_API_KEY']

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    app.logger.info("Request body: " + body)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        app.logger.info("Invalid signature. Please check your channel access token/channel secret.")
        abort(400)

    return 'OK'

@app.route('/checkin', methods=['POST'])
def checkin():
    data = request.get_json()
    latitude = data.get('latitude')
    longitude = data.get('longitude')
    timestamp = data.get('timestamp')
    user_profile = data.get('userProfile')

    print('Received check-in data:', data)

    if not all([latitude, longitude, timestamp, user_profile]):
        return jsonify({"error": "Missing data"}), 400

    utc_time = datetime.strptime(timestamp, '%Y-%m-%dT%H:%M:%S.%fZ')
    local_tz = pytz.timezone('Asia/Taipei')
    local_time = utc_time.replace(tzinfo=pytz.utc).astimezone(local_tz)
    local_time_str = local_time.strftime('%Y-%m-%dT%H:%M:%S.%f%z')

    checkin_record = {
        "latitude": latitude,
        "longitude": longitude,
        "timestamp": local_time_str
    }

    try:
        checkins_collection.update_one(
            {"_id": user_profile["userId"]},
            {
                "$set": {
                    "displayName": user_profile["displayName"],
                    "pictureUrl": user_profile["pictureUrl"]
                },
                "$push": {
                    "checkins": checkin_record
                }
            },
            upsert=True
        )
        print('Check-in data saved to MongoDB')
    except Exception as e:
        print('Error inserting data into MongoDB:', e)
        return jsonify({"error": str(e)}), 500

    return jsonify({"message": "Check-in saved successfully"}), 200

@app.route('/fetch_checkins', methods=['POST'])
def fetch_checkins():
    try:
        checkins = list(checkins_collection.find({}, {'_id': 0}))
        print('Checkins data:', checkins)
        return jsonify(checkins), 200
    except Exception as e:
        print('Error fetching check-ins:', e)
        return jsonify({"error": str(e)}), 500

@app.route('/checkins', methods=['DELETE'])
def delete_checkins():
    result = checkins_collection.delete_many({})
    if result.deleted_count > 0:
        return jsonify({"message": "All check-ins deleted successfully"}), 200
    else:
        return jsonify({"error": "No check-ins found to delete"}), 404
    
@app.route('/checkin/timestamp', methods=['POST'])
def get_timestamp():
    data = request.get_json()
    user_id = data.get('userId')
    print('Received data:', data)  # 添加日志
    print('user_id:', user_id)  # 添加日志

    user_checkins = checkins_collection.find_one({'_id': user_id}, {'checkins': 1})
    print('user_checkins:', user_checkins)  # 添加日志

    if user_checkins and 'checkins' in user_checkins and user_checkins['checkins']:
        latest_checkin = user_checkins['checkins'][-1]  # 获取最新的打卡记录
        timestamp = latest_checkin.get('timestamp')
        return jsonify({"timestamp": timestamp})
    else:
        return jsonify({"error": "No check-ins found for user"}), 404


@app.route('/favicon.ico')
def favicon():
    return '', 404

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

@app.route('/get_itineraries', methods=['POST'])
def get_itineraries():
    user_id = request.json.get('user_id')
    
    if not user_id:
        return jsonify({'status': 'error', 'message': '需要提供使用者ID'}), 400

    try:
        user = users.find_one({"_id": user_id})
        if not user:
            return jsonify({'status': 'error', 'message': '找不到使用者'}), 404

        itineraries = user.get('itineraries', [])
        return jsonify({'status': 'success', 'itineraries': itineraries})
    except Exception as e:
        print(f'獲取使用者行程時發生錯誤: {e}')
        return jsonify({'status': 'error', 'message': f'獲取使用者行程時發生錯誤: {str(e)}'}), 500

@app.route('/add_itinerary', methods=['POST'])
def add_itinerary():
    data = request.json
    user_id = data.get('user_id')
    itinerary = data.get('itinerary')

    print(f"Received itinerary: {itinerary} for user: {user_id}")

    try:
        result = users.update_one(
            {"_id": user_id},
            {"$push": {"itineraries": itinerary}}
        )

        if result.modified_count > 0:
            return jsonify({'status': 'success'})
        else:
            return jsonify({'status': 'error', 'message': 'Failed to add itinerary'}), 500
    except Exception as e:
        print(f'Error adding itinerary: {e}')
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/delete_itinerary', methods=['POST'])
def delete_itinerary():
    user_id = request.json.get('user_id')
    itinerary_id = request.json.get('itinerary_id')
    print(f"接收到的 user_id: {user_id}, itinerary_id: {itinerary_id}")

    if not user_id or not itinerary_id:
        return jsonify({'status': 'error', 'message': '需要提供使用者ID和行程ID'}), 400

    try:
        user = users.find_one({"_id": user_id})
        if not user:
            return jsonify({'status': 'error', 'message': '找不到使用者'}), 404

        updated_itineraries = [it for it in user.get('itineraries', []) if it['itinerary_id'] != itinerary_id]
        users.update_one({"_id": user_id}, {"$set": {"itineraries": updated_itineraries}})
        return jsonify({'status': 'success', 'message': '行程已刪除'})
    except Exception as e:
        print(f'刪除行程時發生錯誤: {e}')
        return jsonify({'status': 'error', 'message': f'刪除行程時發生錯誤: {str(e)}'}), 500
    
@app.route('/add_place', methods=['POST'])
def add_place():
    data = request.json
    itinerary_id = data.get('itinerary_id')
    place = data.get('place')

    if not itinerary_id or not place:
        return jsonify({'status': 'error', 'message': '缺少行程ID或地點信息'}), 400

    try:
        user = users.find_one({"itineraries.itinerary_id": itinerary_id})
        if not user:
            return jsonify({'status': 'error', 'message': '找不到行程'}), 404

        users.update_one(
            {"itineraries.itinerary_id": itinerary_id},
            {"$push": {"itineraries.$.places": place}}
        )

        return jsonify({'status': 'success'}), 200
    except Exception as e:
        print(f'添加地点时发生错误: {e}')
        return jsonify({'status': 'error', 'message': f'添加地点时发生错误: {str(e)}'}), 500

@app.route('/route/<user_id>/<itinerary_id>', methods=['GET'])
def route(user_id, itinerary_id):
    user = users.find_one({"_id": user_id})
    if not user:
        return jsonify({'error': 'User not found'}), 404
    
    itinerary = next((it for it in user['itineraries'] if it['itinerary_id'] == itinerary_id), None)
    if not itinerary:
        return jsonify({'error': 'Itinerary not found'}), 404

    places = itinerary['places']
    if len(places) < 2:
        return jsonify({'error': 'At least two places are required.'}), 400

    origin = places[0]
    destination = places[-1]
    waypoints = places[1:-1]

    url = 'https://maps.googleapis.com/maps/api/directions/json'
    params = {
        'origin': f'{origin["latitude"]},{origin["longitude"]}',
        'destination': f'{destination["latitude"]},{destination["longitude"]}',
        'waypoints': '|'.join([f'{wp["latitude"]},{wp["longitude"]}' for wp in waypoints]),
        'key': GOOGLE_MAPS_API_KEY,
        'optimizeWaypoints': 'true'
    }

    response = requests.get(url, params=params)
    if response.status_code != 200:
        return jsonify({'error': 'Failed to get directions from Google Maps API.'}), 500

    directions = response.json()
    if directions['status'] != 'OK':
        return jsonify({'error': 'No route found.'}), 404

    waypoint_order = directions['routes'][0]['waypoint_order']
    optimized_places = [origin] + [waypoints[i] for i in waypoint_order] + [destination]

    users.update_one(
        {"_id": user_id, "itineraries.itinerary_id": itinerary_id},
        {"$set": {"itineraries.$.places": optimized_places}}
    )

    return jsonify(directions['routes'][0])

if __name__ == "__main__":
    app.run(debug=True)
