import json
import requests
import math


with open('env.json') as f:
    env = json.load(f)

api_key = env['API_KEY']
weather_url = f'https://opendata.cwa.gov.tw/api/v1/rest/datastore/O-A0001-001?Authorization={api_key}'

def haversine(lon1, lat1, lon2, lat2):
    R = 6371.0  # 地球半徑（公里）
    dlon = math.radians(lon2 - lon1)
    dlat = math.radians(lat2 - lat1)
    a = math.sin(dlat / 2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    distance = R * c
    return distance

def get_nearest_station(lat, lon):
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
                '縣市': nearest_station['GeoInfo']['CountyName'],
                '鄉鎮': nearest_station['GeoInfo']['TownName'],
                '天氣': nearest_station['WeatherElement']['Weather'],
                '降雨量': nearest_station['WeatherElement']['Now']['Precipitation'],
                '氣溫': nearest_station['WeatherElement']['AirTemperature'],
            }
            return weather_info
        return "無法找到最近的天氣站"
    except KeyError:
        return "數據結構錯誤，無法提取天氣資訊"
