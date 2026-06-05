import os
import time
import json
import tempfile
import asyncio
import gc  # 🔧 新增：用來強制回收記憶體
from datetime import datetime

from fastapi import FastAPI, File, UploadFile, Form, BackgroundTasks, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import gspread
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google import genai
from google.genai import types

# ═══════════════════════════════════════════════════
# 系統環境變數與常數設定
# ═══════════════════════════════════════════════════
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID")
DRIVE_FOLDER_ID = os.environ.get("DRIVE_FOLDER_ID")
MODEL_NAME = "gemini-2.5-flash"

app = FastAPI()

# 🔧 新增：紅綠燈排隊機制。限制同時最多只能有 2 個任務運行，保護 512MB 記憶體
MAX_CONCURRENT_UPLOADS = 4
upload_semaphore = asyncio.Semaphore(MAX_CONCURRENT_UPLOADS)

ALLOWED_URL = "https://use5566.github.io" 

app.add_middleware(
    CORSMiddleware,
    allow_origins=[ALLOWED_URL], 
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
async def root():
    return {"message": "Silomo API 伺服器運作正常！🐾"}

# ═══════════════════════════════════════════════════
# 授權與認證函式
# ═══════════════════════════════════════════════════
def get_google_credentials():
    creds_str = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    if not creds_str:
        raise ValueError("伺服器缺少 GOOGLE_CREDENTIALS_JSON 環境變數")
    
    creds_dict = json.loads(creds_str)
    scopes = [
        'https://www.googleapis.com/auth/spreadsheets',
        'https://www.googleapis.com/auth/drive'
    ]
    return service_account.Credentials.from_service_account_info(creds_dict, scopes=scopes)

def get_gspread_client():
    creds = get_google_credentials()
    return gspread.authorize(creds)

def get_drive_service():
    creds = get_google_credentials()
    return build('drive', 'v3', credentials=creds)

# ═══════════════════════════════════════════════════
# 背景任務：上傳至 Google Drive
# ═══════════════════════════════════════════════════
def upload_to_drive_background(file_path: str, file_name: str, mime_type: str):
    try:
        drive_service = get_drive_service()
        file_metadata = {'name': file_name, 'parents': [DRIVE_FOLDER_ID]}
        media = MediaFileUpload(file_path, mimetype=mime_type, resumable=True)
        
        drive_service.files().create(
            body=file_metadata, 
            media_body=media, 
            fields='id',
            supportsAllDrives=True 
        ).execute()
        
        print(f"✅ Google Drive 備份成功: {file_name}")
    except Exception as e:
        print(f"❌ Google Drive 備份失敗: {e}")
    finally:
        if os.path.exists(file_path):
            os.remove(file_path)

# ═══════════════════════════════════════════════════
# Gemini 處理函式
# ═══════════════════════════════════════════════════
def upload_video_to_gemini(client, file_path: str, mime_type: str, display_name: str):
    video_file = client.files.upload(
        file=file_path,
        config=types.UploadFileConfig(mime_type=mime_type, display_name=display_name)
    )
    
    attempts = 0
    while video_file.state.name == "PROCESSING" and attempts < 20:
        time.sleep(3)
        video_file = client.files.get(name=video_file.name)
        attempts += 1
        
    if video_file.state.name == "FAILED":
        raise Exception("Gemini 影片處理失敗")
    if video_file.state.name == "PROCESSING":
        raise Exception("影片處理時間過長")
        
    return video_file

def call_gemini(system_instruction: str, contents: list):
    client = genai.Client(api_key=GEMINI_API_KEY)
    config = types.GenerateContentConfig(
        temperature=0.2,
        response_mime_type="application/json",
        system_instruction=system_instruction
    )
    
    response = client.models.generate_content(
        model=MODEL_NAME,
        contents=contents,
        config=config
    )
    
    usage = response.usage_metadata
    return {
        "text": response.text,
        "input_tokens": usage.prompt_token_count if usage else 0,
        "output_tokens": usage.candidates_token_count if usage else 0
    }

def clean_json_response(text: str):
    text = text.strip()
    if text.startswith("```json"):
        text = text[7:]
    if text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    return text.strip()

# ═══════════════════════════════════════════════════
# API 路由 1：接收影片與初始分析
# ═══════════════════════════════════════════════════
@app.post("/analyze-video")
async def analyze_video(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    classNum: str = Form(...),
    members: str = Form(...),
    videoName: str = Form(...)
):
    try:
        # 🔧 新增：進入排隊管制區，超過 2 人同時上傳就在這裡等待
        async with upload_semaphore:
            
            ext = os.path.splitext(file.filename)[1]
            with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as temp_file:
                while content := await file.read(1024 * 1024): 
                    temp_file.write(content)
                temp_path = temp_file.name

            background_tasks.add_task(upload_to_drive_background, temp_path, videoName, file.content_type)

            client = genai.Client(api_key=GEMINI_API_KEY)
            gemini_file = upload_video_to_gemini(client, temp_path, file.content_type, videoName)

            system_instruction = (
                "你是一隻名叫 Silomo 的白色波斯貓，也是一位「專業的科學教育專家」。你的任務是引導國小生思考。\n"
                "【重要】嚴格使用 JSON 格式：{\"summary\": \"說明\", \"details\": [{\"point\": \"標題\", \"description\": \"內容\"}]}\n"
                "【警告】必須「極度精簡」，details 最多 3 點，每點 20-30 字以內，絕對不可長篇大論！\n"
                "【特別注意】分析影片時，只要白煙有符合理論的移動趨勢即視為對流發生，不須要求完美的封閉循環，也不須批評器材。如果學生問了與科學實驗無關的話題，不要回答。"
            )
            
            prompt = (
                f"這是一部名為「{videoName}」的氣體對流實驗影片。紅色點的瓶子代表熱空氣(熱瓶)；藍色點的瓶子代表冷空氣(冷瓶)，其中一瓶會有煙。\n"
                "理論組合：\n"
                "(1) 熱瓶在上加煙、冷瓶在下無煙：不對流。煙微沉。\n"
                "(2) 熱瓶在下加煙、冷瓶在上無煙：會對流。煙向上。\n"
                "(3) 熱瓶在上無煙、冷瓶在下加煙：不對流。煙不動。\n"
                "(4) 熱瓶在下無煙、冷瓶在上加煙：會對流。煙向下。\n\n"
                "請判斷實驗屬於哪種配置、白煙是否符合理論，並給予簡短正向回饋。"
            )

            contents = [
                types.Part.from_uri(file_uri=gemini_file.uri, mime_type=gemini_file.mime_type),
                prompt
            ]

            ai_res = call_gemini(system_instruction, contents)
            
            clean_text = clean_json_response(ai_res["text"])
            try:
                ai_data = json.loads(clean_text)
                summary = ai_data.get("summary", "分析完成！")
                details = ai_data.get("details", [])
                
                details_text = " / ".join([f"【{d.get('point','')}】{d.get('description','')}" for d in details])
                sheet_reply = f"{summary}｜{details_text}" if details else summary
                
                html_details = "".join([f"<li><b>{d.get('point','')}：</b>{d.get('description','')}</li>" for d in details])
                display_text = f"<p>{summary}</p><ul>{html_details}</ul>" if details else f"<p>{summary}</p>"
            except:
                sheet_reply = clean_text
                display_text = f"<p>{clean_text}</p>"

            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            row_data = [
                timestamp, classNum, members, videoName, sheet_reply,
                "", "", "", "", "", "", "", "", ai_res["input_tokens"], ai_res["output_tokens"]
            ]
            
            gc_client = get_gspread_client()
            sheet = gc_client.open_by_key(SPREADSHEET_ID).sheet1
            sheet.append_row(row_data)

            return {
                "status": "success",
                "video_analysis_html": display_text,
                "video_analysis_text": sheet_reply,
                "gemini_file": {"uri": gemini_file.uri, "mimeType": gemini_file.mime_type}
            }

    except Exception as e:
        return {"status": "error", "message": str(e)}
    
    finally:
        # 🔧 新增：無論成功或失敗，結束時強制倒垃圾，釋放記憶體
        gc.collect()

# ═══════════════════════════════════════════════════
# API 路由 2：處理聊天
# ═══════════════════════════════════════════════════
class ChatRequest(BaseModel):
    userInfo: dict
    message: str
    turnCount: int
    conversationHistory: list

@app.post("/chat")
async def chat(req: ChatRequest):
    try:
        system_instruction = (
            "你是一隻名叫 Silomo 的波斯貓，也是科學教育專家。\n"
            "嚴格使用 JSON：{\"summary\": \"說明\", \"details\": [{\"point\": \"標題\", \"description\": \"內容\"}]}\n"
            "必須「極度精簡」，details 最多 3 點，每點 20-30 字以內。"
        )

        contents = []
        gemini_file = req.userInfo.get("geminiFile")
        
        for idx, item in enumerate(req.conversationHistory):
            parts = []
            
            if idx == 0 and item["role"] == "user" and gemini_file:
                parts.append(
                    types.Part.from_uri(
                        file_uri=gemini_file["uri"], 
                        mime_type=gemini_file["mimeType"]
                    )
                )
            
            parts.append(types.Part.from_text(text=item["parts"]))
            role = "user" if item["role"] == "user" else "model"
            contents.append(types.Content(role=role, parts=parts))
            
        ai_res = call_gemini(system_instruction, contents)
        
        clean_text = clean_json_response(ai_res["text"])
        try:
            ai_data = json.loads(clean_text)
            summary = ai_data.get("summary", "")
            details = ai_data.get("details", [])
            details_text = " / ".join([f"【{d.get('point','')}】{d.get('description','')}" for d in details])
            sheet_reply = f"{summary}｜{details_text}" if summary else details_text
        except:
            sheet_reply = clean_text

        gc_client = get_gspread_client()
        sheet = gc_client.open_by_key(SPREADSHEET_ID).sheet1
        all_records = sheet.get_all_values()
        
        target_row = -1
        for i in range(len(all_records) - 1, -1, -1):
            row = all_records[i]
            if len(row) >= 4 and row[1] == req.userInfo["classNum"] and row[2] == req.userInfo["members"] and row[3] == req.userInfo["videoName"]:
                target_row = i + 1 
                break
                
        if target_row != -1:
            in_tok = ai_res["input_tokens"]
            out_tok = ai_res["output_tokens"]
            
            if req.turnCount == 1:
                sheet.update(f"F{target_row}:I{target_row}", [[req.message, sheet_reply, in_tok, out_tok]])
            elif req.turnCount == 2:
                sheet.update(f"J{target_row}:M{target_row}", [[req.message, sheet_reply, in_tok, out_tok]])
                
            current_in = int(all_records[target_row-1][13]) if len(all_records[target_row-1]) > 13 and all_records[target_row-1][13] else 0
            current_out = int(all_records[target_row-1][14]) if len(all_records[target_row-1]) > 14 and all_records[target_row-1][14] else 0
            sheet.update(f"N{target_row}:O{target_row}", [[current_in + in_tok, current_out + out_tok]])

        return {
            "status": "success",
            "ai_response": clean_text
        }

    except Exception as e:
        return {"status": "error", "message": str(e)}
        
    finally:
        # 🔧 新增：聊天結束也順手倒垃圾
        gc.collect()
