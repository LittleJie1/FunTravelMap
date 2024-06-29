from flask import Flask, request, jsonify, abort
from flask_cors import CORS
import json
from time import strftime
from pymongo.mongo_client import MongoClient
import requests  
import uuid
import pytz
import googlemaps
import os
from geopy.distance import geodesic
from google.cloud import storage
from vertexai.preview.generative_models import GenerativeModel
from datetime import datetime
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
    TextMessage,
    FlexMessage,
    FlexContainer
)
from linebot.v3.webhooks import (
    MessageEvent,
    TextMessageContent,
    FollowEvent,
    UnfollowEvent,
    LocationMessageContent
)
from utils import (
    get_nearest_station,
    calculate_distance_matrix,
    extract_distances,
    find_best_route,
    get_places_by_city,
    filter_high_rated_places,
    is_nearby
)

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

# 讀取環境變數
with open('env.json') as f:
    env = json.load(f)

configuration = Configuration(access_token=env['CHANNEL_ACCESS_TOKEN'])
handler = WebhookHandler(env['CHANNEL_SECRET'])

api_key = env['API_KEY']
weather_url = f"https://opendata.cwa.gov.tw/api/v1/rest/datastore/O-A0001-001?Authorization={api_key}"

icon_base_url = "https://storage.googleapis.com/funtravelmap/weather_icon/"
weather_icons = {
    "晴": "sun.png",
    "多雲": "clouds.png",
    "陰": "cloudy.png",
    "多雲有雨": "rain.png",
    "陰有雷": "storm.png",
    "陰有雨": "rain_cloudy.png",
    "陰有雷雨": "rain_thunder.png"
}

# 設置 MongoDB 連接
mongo_client = MongoClient(env['MONGODB_URI'])

try:
    mongo_client.admin.command('ping')
    print("Pinged your deployment. You successfully connected to MongoDB!")
except Exception as e:
    print(e)

db = mongo_client['web']
users = db['travel']
GOOGLE_MAPS_API_KEY = env['GOOGLE_MAPS_API_KEY']

# Google Maps API 金鑰
API_KEY = GOOGLE_MAPS_API_KEY
# 初始化 googlemaps 客戶端
gmaps = googlemaps.Client(key=API_KEY)
# 設置Google Application Credentials環境變量
os.environ['GOOGLE_APPLICATION_CREDENTIALS'] = 'teamwork.json'
# 初始化Gemini模型
model = GenerativeModel("gemini-1.5-pro-preview-0409")
# 設定生成配置
generation_config = {
    "temperature": 1,
    "top_k": 40,
    "top_p": 0.9
}

# 設置Google Cloud Storage客戶端
# os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = "gcs-key.json"
gcs_client = storage.Client()
bucket_name = 'funtravelmap' # 你的存儲桶名稱
bucket = gcs_client.bucket(bucket_name)

@app.route("/api/callback", methods=['POST'])
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
# ---------------------------------------------------------------      weather
@handler.add(MessageEvent, message=LocationMessageContent)
def handle_location_message(event):
    latitude = event.message.latitude
    longitude = event.message.longitude
    weather_info = get_nearest_station(latitude, longitude, weather_url)
    
    if isinstance(weather_info, dict):
        weather = weather_info['天氣']
        icon_filename = weather_icons.get(weather, "default.png")
        icon_url = f"{icon_base_url}{icon_filename}"

        with open('flex_message_template.json', encoding='utf-8') as f:
            flex_template = json.load(f)

        flex_template_str = json.dumps(flex_template)
        flex_template_str = flex_template_str.replace("${city}", weather_info['縣市'])
        flex_template_str = flex_template_str.replace("${town}", weather_info['鄉鎮'])
        flex_template_str = flex_template_str.replace("${weather}", weather)
        flex_template_str = flex_template_str.replace("${icon_url}", icon_url)
        flex_template_str = flex_template_str.replace("${temperature}", str(weather_info['氣溫']))
        flex_template_str = flex_template_str.replace("${rainfall}", str(weather_info['降雨量']))
        flex_template = json.loads(flex_template_str)

        msg = FlexMessage(
            alt_text="天氣資訊",
            contents=FlexContainer.from_json(json.dumps(flex_template))
        )
    else:
        msg = TextMessage(text=weather_info)
        
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.reply_message_with_http_info(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[msg]
            )
        )

# ---------------------------------------------------------------     weather

@app.route('/api/get_itineraries', methods=['POST']) #-------------------------查看行程
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


@app.route('/api/add_itinerary', methods=['POST'])  # -------------------------新建行程
def add_itinerary():
    data = request.json
    user_id = data.get('user_id')
    itinerary = data.get('itinerary')
    itinerary_id = itinerary.get('itinerary_id')
    itinerary_name = itinerary.get('name')
    days = itinerary.get('days')

    # 檢查必要的字段是否存在
    if not all([user_id, itinerary_id, itinerary_name, days]):
        print("Missing required fields")
        return jsonify({'status': 'error', 'message': 'Missing required fields'}), 400
    # 確保 days 是整數
    try:
        days = int(days)
    except ValueError:
        print("Invalid days value")
        return jsonify({'status': 'error', 'message': 'Invalid days value'}), 400
    # 初始化行程，每天的景點列表为空
    itinerary = {
        "itinerary_id": itinerary_id,
        "name": itinerary_name,
        "days": days,
        "places": [[] for _ in range(days)]
    }
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
    
@app.route('/api/delete_itinerary', methods=['POST']) #--------------------刪除行程
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

        # 找到並刪除對應的行程
        updated_itineraries = [it for it in user.get('itineraries', []) if it['itinerary_id'] != itinerary_id]
        users.update_one({"_id": user_id}, {"$set": {"itineraries": updated_itineraries}})
        return jsonify({'status': 'success', 'message': '行程已刪除'})
    except Exception as e:
        print(f'刪除行程時發生錯誤: {e}')
        return jsonify({'status': 'error', 'message': f'刪除行程時發生錯誤: {str(e)}'}), 500
    
@app.route('/api/add_place', methods=['POST'])  # --------------------加入行程
def add_place():
    data = request.json
    itinerary_id = data.get('itinerary_id')
    day_index = data.get('day_index')
    place = data.get('place')
    if not itinerary_id or day_index is None or not place:
        return jsonify({'status': 'error', 'message': '缺少行程ID或地點信息或天數索引'}), 400
    try:
        user = users.find_one({"itineraries.itinerary_id": itinerary_id})
        if not user:
            return jsonify({'status': 'error', 'message': '找不到行程'}), 404
        # 查找具體的行程
        itinerary = None
        for it in user['itineraries']:
            if it['itinerary_id'] == itinerary_id:
                itinerary = it
                break
        if not itinerary:
            return jsonify({'status': 'error', 'message': '找不到行程'}), 404
        # 確保 'places' 是一個包含多個子數組的列表，每個子數組代表一天的行程
        if not isinstance(itinerary['places'], list):
            itinerary['places'] = []

        # 初始化每一天的行程為一個列表
        while len(itinerary['places']) <= day_index:
            itinerary['places'].append([])

        # 將新的地點添加到指定的天數
        itinerary['places'][day_index].append(place)

        # 更新 MongoDB 中的用戶文檔
        users.update_one(
            {"_id": user['_id'], "itineraries.itinerary_id": itinerary_id},
            {"$set": {"itineraries.$.places": itinerary['places']}}
        )
        return jsonify({'status': 'success'}), 200

    except Exception as e:
        print(f'添加地點時發生錯誤: {e}')
        return jsonify({'status': 'error', 'message': f'添加地點時發生錯誤: {str(e)}'}), 500
    
@app.route('/api/remove_day', methods=['POST']) # --------------------減天數
def remove_day():
    data = request.json
    itinerary_id = data.get('itinerary_id')

    if not itinerary_id:
        return jsonify({'status': 'error', 'message': '缺少行程ID'}), 400

    try:
        user = users.find_one({"itineraries.itinerary_id": itinerary_id})
        if not user:
            return jsonify({'status': 'error', 'message': '找不到行程'}), 404

        itinerary = next(it for it in user['itineraries'] if it['itinerary_id'] == itinerary_id)
        if itinerary['days'] <= 1:
            return jsonify({'status': 'error', 'message': '行程天數不能少於1天'}), 400

        users.update_one(
            {"itineraries.itinerary_id": itinerary_id},
            {"$inc": {"itineraries.$.days": -1}, "$pop": {"itineraries.$.places": 1}}
        )
        return jsonify({'status': 'success'}), 200
    except Exception as e:
        print(f'刪除天數時發生錯誤: {e}')
        return jsonify({'status': 'error', 'message': f'刪除天數時發生錯誤: {str(e)}'}), 500
    
@app.route('/api/add_day', methods=['POST']) # ------------------------加天數
def add_day():
    data = request.json
    itinerary_id = data.get('itinerary_id')

    if not itinerary_id:
        return jsonify({'status': 'error', 'message': '缺少行程ID'}), 400

    try:
        user = users.find_one({"itineraries.itinerary_id": itinerary_id})
        if not user:
            return jsonify({'status': 'error', 'message': '找不到行程'}), 404

        users.update_one(
            {"itineraries.itinerary_id": itinerary_id},
            {"$inc": {"itineraries.$.days": 1}, "$push": {"itineraries.$.places": []}}
        )
        return jsonify({'status': 'success'}), 200
    except Exception as e:
        print(f'添加天數時發生錯誤: {e}')
        return jsonify({'status': 'error', 'message': f'添加天數時發生錯誤: {str(e)}'}), 500

@app.route('/api/move_place', methods=['POST'])  # --------------------移動景點順序
def move_place():
    data = request.json
    itinerary_id = data.get('itinerary_id')
    day_index = data.get('day_index')
    place_index = data.get('place_index')
    direction = data.get('direction')

    if not all([itinerary_id, day_index is not None, place_index is not None, direction]):
        return jsonify({'status': 'error', 'message': '缺少必要的字段'}), 400

    try:
        user = users.find_one({"itineraries.itinerary_id": itinerary_id})
        if not user:
            return jsonify({'status': 'error', 'message': '找不到行程'}), 404

        itinerary = next(it for it in user['itineraries'] if it['itinerary_id'] == itinerary_id)
        places = itinerary['places'][day_index]

        if direction == 'up' and place_index > 0:
            places.insert(place_index - 1, places.pop(place_index))
        elif direction == 'down' and place_index < len(places) - 1:
            places.insert(place_index + 1, places.pop(place_index))
        else:
            return jsonify({'status': 'error', 'message': '移動方向無效或位置錯誤'}), 400

        users.update_one(
            {"_id": user['_id'], "itineraries.itinerary_id": itinerary_id},
            {"$set": {"itineraries.$.places": itinerary['places']}}
        )
        return jsonify({'status': 'success'}), 200

    except Exception as e:
        print(f'移動地點時發生錯誤: {e}')
        return jsonify({'status': 'error', 'message': f'移動地點時發生錯誤: {str(e)}'}), 500

@app.route('/api/delete_place', methods=['POST'])  # --------------------移動景點順序
def delete_place():
    data = request.json
    itinerary_id = data.get('itinerary_id')
    day_index = data.get('day_index')
    place_index = data.get('place_index')

    if not all([itinerary_id, day_index is not None, place_index is not None]):
        return jsonify({'status': 'error', 'message': '缺少必要的字段'}), 400

    try:
        user = users.find_one({"itineraries.itinerary_id": itinerary_id})
        if not user:
            return jsonify({'status': 'error', 'message': '找不到行程'}), 404

        itinerary = next(it for it in user['itineraries'] if it['itinerary_id'] == itinerary_id)
        places = itinerary['places'][day_index]

        if place_index < 0 or place_index >= len(places):
            return jsonify({'status': 'error', 'message': '地點索引無效'}), 400

        places.pop(place_index)

        users.update_one(
            {"_id": user['_id'], "itineraries.itinerary_id": itinerary_id},
            {"$set": {"itineraries.$.places": itinerary['places']}}
        )
        return jsonify({'status': 'success'}), 200

    except Exception as e:
        print(f'刪除地點時發生錯誤: {e}')
        return jsonify({'status': 'error', 'message': f'刪除地點時發生錯誤: {str(e)}'}), 500
    
 
@app.route('/api/optimize_route', methods=['POST']) # ------------------------------------------實現最短路徑按鈕
def optimize_route():
    data = request.json
    itinerary_id = data.get('itinerary_id')
    day_index = data.get('day_index')

    if not all([itinerary_id, day_index is not None]):
        return jsonify({'status': 'error', 'message': '缺少必要的字段'}), 400

    user = users.find_one({"itineraries.itinerary_id": itinerary_id})
    if not user:
        return jsonify({'status': 'error', 'message': '找不到行程'}), 404

    itinerary = next(it for it in user['itineraries'] if it['itinerary_id'] == itinerary_id)
    places = itinerary['places'][day_index]

    if len(places) < 2:
        return jsonify({'status': 'error', 'message': '地點數量不足'}), 400

    origins = '|'.join([f"{place['latitude']},{place['longitude']}" for place in places])

    try:
        response_data = calculate_distance_matrix(origins, GOOGLE_MAPS_API_KEY)
        if response_data['status'] != 'OK':
            return jsonify({'status': 'error', 'message': 'Google API錯誤'}), 500

        distances = extract_distances(response_data)
        sorted_places = find_best_route(distances, places)

        # 更新 MongoDB 中的行程順序
        users.update_one(
            {"_id": user['_id'], "itineraries.itinerary_id": itinerary_id},
            {"$set": {f"itineraries.$.places.{day_index}": sorted_places}}
        )
        return jsonify({'status': 'success', 'route': sorted_places}), 200

    except Exception as e:
        print(f'優化路徑時發生錯誤: {e}')
        return jsonify({'status': 'error', 'message': f'優化路徑時發生錯誤: {str(e)}'}), 500

@app.route('/api/update_place_order', methods=['POST'])# ------------------------------------------拖曳方式移動景點順序
def update_place_order():
    data = request.json
    itinerary_id = data.get('itinerary_id')
    day_index = data.get('day_index')
    places = data.get('places')

    # 修改檢查邏輯，允許 places 為空數組
    if not all([itinerary_id, day_index is not None]):
        return jsonify({'status': 'error', 'message': '缺少必要的字段'}), 400

    try:
        user = users.find_one({"itineraries.itinerary_id": itinerary_id})
        if not user:
            return jsonify({'status': 'error', 'message': '找不到行程'}), 404

        itinerary = next(it for it in user['itineraries'] if it['itinerary_id'] == itinerary_id)
        itinerary['places'][day_index] = places

        users.update_one(
            {"_id": user['_id'], "itineraries.itinerary_id": itinerary_id},
            {"$set": {f"itineraries.$.places.{day_index}": places}}
        )
        return jsonify({'status': 'success'}), 200

    except Exception as e:
        print(f'更新地點順序時發生錯誤: {e}')
        return jsonify({'status': 'error', 'message': f'更新地點順序時發生錯誤: {str(e)}'}), 500
    
@app.route('/api/process_city_selection', methods=['POST'])# ------------------------------------------智能推薦景點
def process_city_selection():
    data = request.json
    city_name = data.get('city_name')
    itinerary_id = data.get('itinerary_id')
    day_index = data.get('day_index')

    if not all([city_name, itinerary_id, day_index is not None]):
        return jsonify({'status': 'error', 'message': '缺少必要的字段'}), 400

    try:
        # 查詢指定縣市的景點
        print("查詢指定縣市的景點")
        places = get_places_by_city(gmaps, city_name)
        high_rated_places = filter_high_rated_places(places)
        print(f"查詢結果: {len(high_rated_places)} 個高評價景點")

        # 準備景點信息列表
        places_list = [
            {
                "place_id": place['place_id'],
                "name": place['name'],
                "latitude": place['geometry']['location']['lat'],
                "longitude": place['geometry']['location']['lng'],
                "address": place.get('formatted_address', place.get('vicinity', '')),
                "visited": False
            }
            for place in high_rated_places
        ]

        # 調用 Gemini API
        print("調用 Gemini API")
        prompt = '''
        請依據我給你JSON景點內容，依照我給你的條件回覆我
        1. 從JSON裡面挑選出五個你覺得推薦且值得拜訪的景點，不可以從前面開始選，一定要依照我給的資料中隨機選取
        2. 如果name有顯示單獨縣市名稱、停車場相關，廁所相關都不列入你的選項
        3. 請勿回復其他訊息
        4. 以我傳給你的JSON樣式保持原樣，回覆我你排的順序就好，每次都可以不一樣
        '''
        prompt += json.dumps(places_list, ensure_ascii=False, indent=4)
        r = model.generate_content(
            [prompt],
            generation_config=generation_config
        )

        # 確保回應為有效的 JSON
        print("處理 Gemini 回應")
        if isinstance(r.text, str):
            try:
                gemini_response = json.loads(r.text.strip())
            except json.JSONDecodeError:
                return jsonify({'status': 'error', 'message': 'Gemini 回應無效的 JSON'}), 500

            # 調用最佳路線計算
            print("調用最佳路線計算")
            origins = '|'.join([f"{place['latitude']},{place['longitude']}" for place in gemini_response])
            response_data = calculate_distance_matrix(origins, GOOGLE_MAPS_API_KEY)
            if response_data['status'] != 'OK':
                print(f"Google API 錯誤: {response_data['status']}")
                return jsonify({'status': 'error', 'message': 'Google API錯誤'}), 500

            distances = extract_distances(response_data)
            sorted_places = find_best_route(distances, gemini_response)
            print(f"最佳路線計算結果: {sorted_places}")

            # 更新 MongoDB
            print("更新 MongoDB")
            user = users.find_one({"itineraries.itinerary_id": itinerary_id})
            if not user:
                print("找不到行程")
                return jsonify({'status': 'error', 'message': '找不到行程'}), 404

            users.update_one(
                {"_id": user['_id'], "itineraries.itinerary_id": itinerary_id},
                {"$set": {f"itineraries.$.places.{day_index}": sorted_places}}
            )
            print("更新 MongoDB 成功")

            return jsonify({'status': 'success', 'places': sorted_places}), 200

        else:
            return jsonify({'status': 'error', 'message': 'Gemini API 回應格式錯誤'}), 500

    except Exception as e:
        print(f'處理縣市選擇時發生錯誤: {e}')
        return jsonify({'status': 'error', 'message': f'處理縣市選擇時發生錯誤: {str(e)}'}), 500
    
# ------------------------------------------------------------------------------ raman part
# 添加在 checkin 函數之前，定義一個函數，用於檢查用戶是否已經在某個地點打卡
@app.route('/api/check_nearby_places', methods=['POST'])
def check_nearby_places():
    data = request.get_json()
    latitude = data.get('latitude')
    longitude = data.get('longitude')
    user_profile = data.get('userProfile')

    if not all([latitude, longitude, user_profile]):
        return jsonify({"error": "Missing data"}), 400

    try:
        user = users.find_one({"_id": user_profile["userId"]})
        if not user or 'itineraries' not in user:
            return jsonify([]), 200

        nearby_places = []
        for itinerary in user['itineraries']:
            for day in itinerary.get('places', []):
                for place in day:
                    distance = geodesic((place['latitude'], place['longitude']), (latitude, longitude)).km
                    if distance <= 1:
                        nearby_places.append(place)

        return jsonify(nearby_places), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    
#打卡功能
@app.route('/api/checkin', methods=['POST'])
def checkin():
    data = request.get_json()
    latitude = data.get('latitude')
    longitude = data.get('longitude')
    timestamp = data.get('timestamp')
    user_profile = data.get('userProfile')
    checkin_name = data.get('checkinName', '未命名')  # 使用傳遞過来的 checkinName 或默認值
    selected_place_id = data.get('selectedPlaceId', None)

    if not all([latitude, longitude, timestamp, user_profile]):
        return jsonify({"error": "Missing data"}), 400

    checkin_id = str(uuid.uuid4())
    utc_time = datetime.strptime(timestamp, '%Y-%m-%dT%H:%M:%S.%fZ')
    local_tz = pytz.timezone('Asia/Taipei')
    local_time = utc_time.replace(tzinfo=pytz.utc).astimezone(local_tz)
    local_time_str = local_time.strftime('%Y-%m-%dT%H:%M:%S.%f%z')

    checkin_record = {
        "checkinId": checkin_id,
        "checkinName": checkin_name,  # 使用傳遞過来的 checkinName
        "latitude": latitude,
        "longitude": longitude,
        "timestamp": local_time_str,
        "photos": [],
        "description": "",
        "palseCheckin": False  # 默認为 False
    }

    try:
        # 先保存打卡紀錄
        users.update_one(
            {"_id": user_profile["userId"]},
            {"$push": {"checkins": checkin_record}},
            upsert=True
        )

        
        # 檢查並更新最近的在1公里以內的地點的 `visited` 屬性
        user = users.find_one({"_id": user_profile["userId"]})
        if user and 'itineraries' in user:
            if selected_place_id:
                for itinerary in user['itineraries']:
                    for day in itinerary.get('places', []):
                        for place in day:
                            if place['place_id'] == selected_place_id:
                                place['visited'] = True
                                users.update_one(
                                    {"_id": user_profile["userId"], "itineraries.itinerary_id": itinerary["itinerary_id"]},
                                    {"$set": {f"itineraries.$.places": itinerary['places']}}
                                )
                                checkin_record['palseCheckin'] = True
                                users.update_one(
                                    {"_id": user_profile["userId"], "checkins.checkinId": checkin_id},
                                    {"$set": {"checkins.$.palseCheckin": True}}
                                )

        return jsonify({"checkinId": checkin_id, "palseCheckin": checkin_record['palseCheckin']}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500



@app.route('/api/fetch_checkins', methods=['POST'])  #修改取回打卡數據API，只返回當前用戶的數據
def fetch_checkins():
    user_profile = request.get_json().get('userProfile')
    if not user_profile:
        return jsonify({"error": "Missing user profile"}), 400

    try:
        user_checkins = users.find_one({"_id": user_profile["userId"]}, {"_id": 0, "checkins": 1})
        if user_checkins and "checkins" in user_checkins:
            return jsonify(user_checkins["checkins"]), 200
        else:
            return jsonify([]), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/delete_checkin', methods=['POST'])
def delete_checkin():
    data = request.json
    checkin_id = data.get('checkinId')

    if not checkin_id:
        return jsonify({"error": "Missing checkinId"}), 400

    try:
        user_checkin = users.find_one({'checkins.checkinId': checkin_id})
        if not user_checkin:
            return jsonify({'error': 'Checkin not found'}), 404

        checkin = next((item for item in user_checkin['checkins'] if item['checkinId'] == checkin_id), None)
        if checkin:
            palseCheckin = checkin.get('palseCheckin', False)

            # 删除 Google Cloud Storage 中的文件
            if 'photos' in checkin:
                for photo_url in checkin['photos']:
                    blob_name = photo_url.split(f"https://storage.googleapis.com/{bucket_name}/")[-1]
                    blob = bucket.blob(blob_name)
                    blob.delete()
            # 從用戶的checkins列表中刪除此打卡記錄
            users.update_one(
                {'checkins.checkinId': checkin_id},
                {'$pull': {'checkins': {'checkinId': checkin_id}}}
            )

            if palseCheckin:
                user = users.find_one({"_id": user_checkin["_id"]})
                if user and 'itineraries' in user:
                    for itinerary in user['itineraries']:
                        for day in itinerary.get('places', []):
                            for place in day:
                                if place['latitude'] == checkin['latitude'] and place['longitude'] == checkin['longitude']:
                                    place['visited'] = False
                                    users.update_one(
                                        {"_id": user["_id"], "itineraries.itinerary_id": itinerary["itinerary_id"]},
                                        {"$set": {f"itineraries.$.places": itinerary['places']}}
                                    )
                                    break

            return jsonify({'status': 'success', 'message': 'Check-in deleted successfully'}), 200
        else:
            return jsonify({'error': 'Checkin not found'}), 404
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/checkin/<checkin_id>', methods=['POST'])  # 確保允許 POST 方法#4
def get_checkin(checkin_id):
    try:
        # 根據 checkin_id 查找打卡記錄
        user_checkin = users.find_one({'checkins.checkinId': checkin_id}, {'checkins.$': 1})
        if user_checkin and 'checkins' in user_checkin and user_checkin['checkins']:
            checkin = user_checkin['checkins'][0]
            return jsonify(checkin), 200
        else:
            return jsonify({'error': 'Checkin not found'}), 404
    except Exception as e:
        app.logger.error('Error fetching check-in details: %s', e)
        return jsonify({'error': str(e)}), 500

@app.route('/api/update_checkin', methods=['POST'])
def update_checkin():
    data = request.form
    checkin_id = data.get('checkinId')
    checkin_name = data.get('checkinName')
    description = data.get('description')
    photos = request.files.getlist('photos')  # 處理多張照片
    user_id = data.get('userId')

    if not checkin_id:
        return jsonify({"error": "Missing checkinId"}), 400

    update_data = {}
    if checkin_name:
        update_data["checkins.$.checkinName"] = checkin_name
    if description:
        update_data["checkins.$.description"] = description

    # 檢查現有照片數量
    user_checkin = users.find_one({"checkins.checkinId": checkin_id}, {"checkins.$": 1})
    if user_checkin and 'checkins' in user_checkin and user_checkin['checkins']:
        existing_photos = user_checkin['checkins'][0].get('photos', [])
        if len(existing_photos) + len(photos) > 9:
            return jsonify({"error": "最多只能上傳9張照片"}), 400

    photo_urls = []
    for photo in photos:
        photo_filename = f"{checkin_id}_{photo.filename}"
        folder_path = f"{user_id}/{checkin_id}/"
        blob = bucket.blob(f"{folder_path}{photo_filename}")
        blob.upload_from_file(photo, content_type=photo.content_type)
        photo_url = f"https://storage.googleapis.com/{bucket_name}/{folder_path}{photo_filename}"
        photo_urls.append(photo_url)

    try:
        if photo_urls:
            result = users.update_one(
                {"checkins.checkinId": checkin_id},
                {
                    "$set": update_data,
                    "$push": {"checkins.$.photos": {"$each": photo_urls}}  # 使用 $push 和 $each 追加多張照片
                }
            )
        else:
            result = users.update_one(
                {"checkins.checkinId": checkin_id},
                {"$set": update_data}
            )
        if result.matched_count > 0:
            return jsonify({"message": "Checkin updated successfully"}), 200
        else:
            return jsonify({"error": "Checkin not found"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    
@app.route('/api/update_photo_order', methods=['POST'])
def update_photo_order():
    data = request.json
    checkin_id = data.get('checkinId')
    photo_order = data.get('photoOrder')

    if not all([checkin_id, photo_order]):
        return jsonify({'status': 'error', 'message': '缺少必要的字段'}), 400

    try:
        result = users.update_one(
            {"checkins.checkinId": checkin_id},
            {"$set": {"checkins.$.photos": [photo['url'] for photo in photo_order]}}
        )
        if result.matched_count > 0:
            return jsonify({'status': 'success'}), 200
        else:
            return jsonify({'status': 'error', 'message': '打卡記錄未找到'}), 404
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500
    
@app.route('/api/set_homepage_photo', methods=['POST'])
def set_homepage_photo():
    data = request.json
    checkin_id = data.get('checkinId')
    photo_url = data.get('photoUrl')

    if not checkin_id or not photo_url:
        return jsonify({"error": "Missing data"}), 400

    try:
        user_checkin = users.find_one({'checkins.checkinId': checkin_id})
        if not user_checkin:
            return jsonify({'error': 'Checkin not found'}), 404

        checkin = next((item for item in user_checkin['checkins'] if item['checkinId'] == checkin_id), None)
        if checkin:
            photos = checkin.get('photos', [])
            if not photos:
                return jsonify({'error': 'No photos found in checkin'}), 404

            if photo_url not in photos:
                return jsonify({'error': 'Photo not found in checkin'}), 404

            first_photo = photos[0]
            index = photos.index(photo_url)
            photos[0], photos[index] = photos[index], photos[0]

            users.update_one(
                {'checkins.checkinId': checkin_id},
                {'$set': {'checkins.$.photos': photos}}
            )

            return jsonify({'message': 'Homepage photo set successfully'}), 200
        else:
            return jsonify({'error': 'Checkin not found'}), 404
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/delete_photo', methods=['POST'])
def delete_photo():
    data = request.json
    checkin_id = data.get('checkinId')
    photo_url = data.get('photoUrl')

    if not checkin_id or not photo_url:
        return jsonify({"error": "Missing data"}), 400

    try:
        user_checkin = users.find_one({'checkins.checkinId': checkin_id})
        if not user_checkin:
            return jsonify({'error': 'Checkin not found'}), 404

        checkin = next((item for item in user_checkin['checkins'] if item['checkinId'] == checkin_id), None)
        if checkin:
            if 'photos' in checkin and photo_url in checkin['photos']:
                checkin['photos'].remove(photo_url)
                users.update_one(
                    {'checkins.checkinId': checkin_id},
                    {'$set': {'checkins.$.photos': checkin['photos']}}
                )

                # 删除 Google Cloud Storage 中的文件
                blob_name = photo_url.split(f"https://storage.googleapis.com/{bucket_name}/")[-1]
                blob = bucket.blob(blob_name)
                blob.delete()

                return jsonify({'message': 'Photo deleted successfully'}), 200
            else:
                return jsonify({'error': 'Photo not found in checkin'}), 404
        else:
            return jsonify({'error': 'Checkin not found'}), 404
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/proxy_google_places', methods=['POST'])
def proxy_google_places():
    data = request.json
    place_id = data.get('place_id')
    api_key = data.get('key')

    if not place_id or not api_key:
        return jsonify({'status': 'error', 'message': 'Missing required parameters'}), 400

    google_places_url = f"https://maps.googleapis.com/maps/api/place/details/json?place_id={place_id}&key={api_key}&language=zh-TW"

    try:
        response = requests.get(google_places_url)
        return jsonify(response.json()), response.status_code
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500
# ------------------------------------------------------------------------------ raman part

if __name__ == '__main__':
    app.run()
