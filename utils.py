import itertools
import math
import time
import requests
from geopy.distance import geodesic

def haversine(lon1, lat1, lon2, lat2):
    R = 6371.0  # 地球半徑（公里）
    dlon = math.radians(lon2 - lon1)
    dlat = math.radians(lat2 - lat1)
    a = math.sin(dlat / 2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    distance = R * c
    return distance

def get_nearest_station(lat, lon, weather_url):
    response = requests.get(weather_url)
    data = response.json()
    
    try:
        stations = data['records']['Station']
        nearest_station = None
        min_distance = float('inf')
        
        for station in stations:
            station_lat = station['GeoInfo']['Coordinates'][1]['StationLatitude']
            station_lon = station['GeoInfo']['Coordinates'][1]['StationLongitude']
            distance = haversine(lon, lat, station_lon, station_lat)
            
            if distance < min_distance:
                min_distance = distance
                nearest_station = station
        
        if nearest_station:
            weather_info = {
                '縣市': nearest_station['GeoInfo']['CountyName'] or "未知",
                '鄉鎮': nearest_station['GeoInfo']['TownName'] or "未知",
                '天氣': nearest_station['WeatherElement']['Weather'] or "未知",
                '降雨量': nearest_station['WeatherElement']['Now'].get('Precipitation', "未知"),
                '氣溫': nearest_station['WeatherElement'].get('AirTemperature', "未知")
            }
            return weather_info
        return "無法找到最近的天氣站"
    except KeyError:
        return "數據結構錯誤，無法提取天氣資訊"
    
#-----------------------------------計算景點之間距離矩陣
def calculate_distance_matrix(origins, google_maps_api_key):
    url = f"https://maps.googleapis.com/maps/api/distancematrix/json?origins={origins}&destinations={origins}&key={google_maps_api_key}"
    response = requests.get(url)
    response_data = response.json()
    return response_data

#-----------------------------------提取距離矩陣
def extract_distances(response_data):
    distances = []
    for row in response_data['rows']:
        distances.append([element['distance']['value'] for element in row['elements']])
    return distances

#-----------------------------------計算查找最佳路線
def find_best_route(distances, places):
    num_places = len(places)  # 獲取地點數量
    indices = list(range(num_places))  # 創建一個地點索引的列表 [0, 1, 2, ...]
    min_distance = float('inf')  # 初始化最小距離為正無窮大
    best_permutation = indices  # 初始化最佳排列為地點的原始順序
    cache = {}  # 初始化一個字典用來緩存計算過的排列組合的總距離

    def calculate_total_distance(permutation):
        # 如果該排列組合的距離已經計算過，直接從緩存中獲取
        if permutation in cache:
            return cache[permutation]
        
        # 計算該排列組合的總距離
        total_distance = sum(distances[permutation[i]][permutation[i+1]] for i in range(len(permutation) - 1))
        
        # 將計算結果存入緩存中
        cache[permutation] = total_distance
        return total_distance

    # 遍歷所有地點的所有排列組合
    for permutation in itertools.permutations(indices):
        total_distance = calculate_total_distance(permutation)  # 計算當前排列的總距離
        
        # 如果當前排列的總距離小於已知最小距離，則更新最小距離和最佳排列
        if total_distance < min_distance:
            min_distance = total_distance
            best_permutation = permutation

    # 根據最佳排列重新排序地點
    sorted_places = [places[i] for i in best_permutation]
    return sorted_places

def get_places_by_city(gmaps, city_name, place_type='tourist_attraction', language='zh-TW', max_places=30):
    try:
        query = f'{place_type} in {city_name}'
        places_result = gmaps.places(query=query, language=language)
        places = places_result['results']
        total_places = len(places)
        
        while 'next_page_token' in places_result and total_places < max_places:
            next_page_token = places_result['next_page_token']
            time.sleep(2)
            places_result = gmaps.places(query=query, language=language, page_token=next_page_token)
            places.extend(places_result['results'])
            total_places = len(places)
            if total_places >= max_places:
                places = places[:max_places]
                break
        
        return places
    
    except Exception as e:
        print(f"Error in get_places_by_city: {e}")
        return []

def filter_high_rated_places(places, min_rating=4.0):
    try:
        high_rated_places = [place for place in places if place.get('rating', 0) >= min_rating]
        return high_rated_places
    except Exception as e:
        print(f"Error in filter_high_rated_places: {e}")
        return []

def is_nearby(place_lat, place_lng, checkin_lat, checkin_lng, distance_km=1):
    place_coords = (place_lat, place_lng)
    checkin_coords = (checkin_lat, checkin_lng)
    return geodesic(place_coords, checkin_coords).km <= distance_km
