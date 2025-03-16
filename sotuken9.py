import mysql.connector
import requests
import json
import yaml
import os
import datetime
import base64

# 設定ファイルの読み込み
base_path = os.path.dirname(os.path.abspath(__file__))
secret_path = os.path.join(base_path, '_secret.yaml')
with open(secret_path) as f:
    config = yaml.safe_load(f)

def get_db_connection():
    return mysql.connector.connect(
        host="localhost",
        user="test_user",
        password="password",
        database="health"
    )

def get_tokens():
    """ tokens テーブルから id, fitbit_id, fitbit_access, fitbit_refresh, tanita_access, tanita_refresh を取得 """
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT id, fitbit_id, fitbit_access, fitbit_refresh, tanita_access, tanita_refresh FROM tokens")
    tokens = cursor.fetchall()
    cursor.close()
    conn.close()
    return tokens

def insert_health_data(user_id, date, steps, weight, fat, height):
    """ health_data に歩数・体重・体脂肪データを保存（同じ日付の場合は更新） """
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO health_data (user_id, date, steps, weight, fat, height)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE steps = VALUES(steps), weight = VALUES(weight), fat = VALUES(fat), height = VALUES(height)
    """, (user_id, date, steps, weight, fat, height))
    conn.commit()
    cursor.close()
    conn.close()

def update_tokens(user_id, access_token, refresh_token):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE tokens
        SET fitbit_access = %s, fitbit_refresh = %s
        WHERE user_id = %s
    """, (access_token, refresh_token, user_id))
    conn.commit()
    cursor.close()
    conn.close()

def refresh_token(user):
    """リフレッシュトークンを使用してアクセストークンとリフレッシュトークンを更新し、データベースに保存"""
    url = "https://api.fitbit.com/oauth2/token"
    
    # client_id と client_secret を Base64 エンコード
    client_id = config["fitbit"]["client_id"]
    client_secret = config["fitbit"]["client_secret"]
    basic_token = base64.b64encode(f"{client_id}:{client_secret}".encode("utf-8")).decode("utf-8")

    headers = {
        "Authorization": f"Basic {basic_token}",
        "Content-Type": "application/x-www-form-urlencoded"
    }

    # リクエストパラメータ
    params = {
        "grant_type": "refresh_token",
        "refresh_token": user["fitbit_refresh"]
    }

    res = requests.post(url, headers=headers, data=params)
    res_data = res.json()

    if "errors" in res_data:
        print(f"Error refreshing token for user {user['fitbit_id']}: {res_data['errors']}")
        return None, None

    new_access_token = res_data["access_token"]
    new_refresh_token = res_data["refresh_token"]
    update_tokens(user["id"], new_access_token, new_refresh_token)
    return new_access_token, new_refresh_token

def request_fitbit_api(user, url):
    """Fitbit API からデータを取得（必要ならトークン更新）"""
    headers = {"Authorization": f"Bearer {user['fitbit_access']}"}
    res = requests.get(url, headers=headers)
    res_data = res.json()

    if "errors" in res_data and any(e.get("errorType") == "expired_token" for e in res_data["errors"]):
        # トークンが期限切れならリフレッシュ
        print(f"Token expired for user {user['id']}, refreshing...")
        access_token = refresh_token(user)
        if not access_token:
            return None
        headers = {"Authorization": f"Bearer {access_token}"}
        res = requests.get(url, headers=headers)
        res_data = res.json()

    return res_data

def request_tanita_api(tanita_access):
    """Tanita API からデータを取得"""
    today = datetime.datetime.now().strftime('%Y%m%d')  # 今日の日付を YYYYMMDD 形式で取得
    from_time = today + "000000"  # 今日の開始時刻（00:00:00）

    payload = {
        'access_token': tanita_access,
        'tag': '6021,6022',  # 体重と体脂肪率を取得
        'date': '1',  # '1'を使う
        'from': from_time,  # 今日の開始時刻を指定
        'to': ''  # 特に指定しない
    }

    res = requests.post("https://www.healthplanet.jp/status/innerscan.json", params=payload)
    res_data = res.json()

    if 'error' in res_data:
        print(f"Error from Tanita API: {res_data['error']}")
        return None
    elif 'data' not in res_data:
        print(f"No data returned from Tanita API for user with token {tanita_access}")
        return None

    weight = 0
    fat = 0
    height = 0

    for item in res_data['data']:
        if 'keydata' in item:
            if item['tag'] == '6021':  # 体重
                weight = float(item['keydata'])
            elif item['tag'] == '6022':  # 体脂肪率
                fat = float(item['keydata'])

    if 'height' in res_data:
        height = float(res_data['height'])  # height を直接取得

    if weight > 0 and fat > 0:
        return {'weight': weight, 'fat': fat, 'height': height}
    else:
        print(f"Weight or fat data not found in Tanita API response: {res_data}")
        return None

def fetch_health_data():
    """ 各ユーザーの Fitbit / Tanita データを取得し、データベースに保存 """
    tokens = get_tokens()

    print(f"Tokens from DB: {tokens}")
    for token in tokens:
        user = {
            "id": token["id"],
            "fitbit_id": token["fitbit_id"],
            "fitbit_access": token["fitbit_access"],
            "fitbit_refresh": token["fitbit_refresh"],
            "tanita_access": token["tanita_access"],
            "tanita_refresh": token["tanita_refresh"]
        }

        steps = 0
        weight = 0
        fat = 0
        height = 0
        date = datetime.datetime.now().strftime('%Y-%m-%d')

        if user["fitbit_access"]:
            steps_url = f"https://api.fitbit.com/1/user/{user['fitbit_id']}/activities/steps/date/today/1d.json"
            steps_data = request_fitbit_api(user, steps_url)
            if steps_data:
                steps = steps_data.get("activities-steps", [{}])[0].get("value", 0)

        if user["tanita_access"]:
            tanita_data = request_tanita_api(user["tanita_access"])
            if tanita_data:
                weight = tanita_data.get('weight', 0)
                fat = tanita_data.get('fat', 0)
                height = tanita_data.get('height', 0)

        insert_health_data(user["id"], date, steps, weight, fat, height)
        print(f"Stored data for user_id {user['id']}: {steps} steps, {weight}kg weight, {fat}% fat, {height}cm height on {date}")

def main():
    fetch_health_data()

if __name__ == "__main__":
    main()
