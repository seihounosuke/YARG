import streamlit as st
from google import genai # 新しいSDK
from google.genai import types
#import mysql.connector
#from mysql.connector import Error
#from streamlit_gsheets import GSheetsConnection
import pandas as pd
import urllib.parse
import re
import io
import os
from dotenv import load_dotenv

load_dotenv()

# --- 初期設定 ---
st.set_page_config(page_title="授業レポート生成AI (PoC)", layout="wide")

# APIクライアント初期化 (新しいSDKの書き方)
client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))


# シート設定

def get_sheet_text(sheet_name):
    # 日本語シート名をURLエンコードしてCSV出力用URLを作成
    encoded_name = urllib.parse.quote(sheet_name)
    url = f"https://docs.google.com/spreadsheets/d/{os.environ.get('DRIVE_SHEETS')}/gviz/tq?tqx=out:csv&sheet={encoded_name}"
    
    try:
        # CSVとして読み込み (ヘッダーなし)
        df = pd.read_csv(url, header=None)
        
        # 全セルを文字列化して結合
        # (NaN/空欄を除去して、すべてのテキストを改行区切りでつなぐ)
        all_text = df.astype(str).stack().str.strip()
        text_content = "\n".join(all_text[all_text != "nan"].tolist())
        
        return text_content
    except Exception as e:
        st.warning(f"⚠️ シート「{sheet_name}」の読み込みに失敗: {e}")
        return ""

@st.cache_data(ttl=600)
def load_learning_data():
    manual_text = get_sheet_text("マニュアル")
    good_examples = get_sheet_text("優良レポート")
    bad_examples = get_sheet_text("不良レポート")

    return manual_text, good_examples, bad_examples

# --- 関数定義 ---

def get_db_connection():
    """MySQLへの接続を確立する"""
    try:
        connection = mysql.connector.connect(**st.secrets["mysql"])#secretsを使うのやめたから動かないけど、まあいまのとこいいか。
        return connection
    except Error as e:
        st.error(f"データベース接続エラー: {e}")
        return None

def get_student_history(student_id):
    """
    生徒IDに基づいて過去のレポートを直近2件取得する。
    エラー時やデータなしの場合は「履歴なし」を返す。
    """
    history_text = "過去のレポート履歴はこの生徒が初回授業のためか、あるいはなんらかの理由で見つかりませんでした。"
    # conn = get_db_connection()
    
    # if conn and conn.is_connected():
    #     try:
    #         cursor = conn.cursor(dictionary=True)
    #         # 安全のためプレースホルダーを使用
    #         query = """
    #             SELECT content, created_at 
    #             FROM reports 
    #             WHERE student_id = %s 
    #             ORDER BY created_at DESC 
    #             LIMIT 2
    #         """
    #         cursor.execute(query, (student_id,))
    #         rows = cursor.fetchall()
            
    #         if rows:
    #             history_list = []
    #             for i, row in enumerate(rows):
    #                 date_str = row['created_at'].strftime('%Y-%m-%d')
    #                 history_list.append(f"--- 過去レポート({date_str}) ---\n{row['content']}")
    #             history_text = "\n\n".join(history_list)
                
    #     except Error as e:
    #         st.warning(f"履歴取得中にエラーが発生しました（処理は続行します）: {e}")
    #     finally:
    #         if conn.is_connected():
    #             cursor.close()
    #             conn.close()
    
    return history_text

def clean_markdown(text):
    """
    Markdown記法（**, ##, > 等）を削除し、LINE送付用のプレーンテキストにする
    """
    # 太字 (**word**) -> word
    text = re.sub(r'\*\*(.*?)\*\*', r'\1', text)
    # 見出し (## Title) -> Title
    text = re.sub(r'#+\s?', '', text)
    # 引用 (> Quote) -> Quote
    text = re.sub(r'>\s?', '', text)
    # 箇条書き (* Item) -> ・Item (LINEで見やすくするため変換)
    text = re.sub(r'^\*\s', '・', text, flags=re.MULTILINE)
    return text.strip()

def generate_report(media_file, student_id, unit_info, history_context, manual_text, good_examples, bad_examples):
    """Gemini 2.5 flashを使用してレポートを生成する"""
    
    # プロンプトの構築
    prompt = f"""
    あなたは保護者からの信頼が厚いプロの塾講師です。
    「過去の指導経緯」を踏まえ、一貫性のある「授業レポート」をLINE用に作成してください。

    【制約事項】
    ・出力はプレーンテキストのみ（Markdown禁止）。
    ・前回指摘した内容が改善されていれば褒め、未達なら再度促すなど、連続性を意識する。
    - 文体は「丁寧・温かい・プロフェッショナル」なトーンで統一してください。
    - 以下のマニュアルと良例・悪例を参考にしてください。
    - 「生徒の様子」の部分を書いてもらうだけなので、長くても200字程度で。
    - また、その生徒が初回授業かどうかがわからない場合はそれに言及しないこと。

    【マニュアル】
    {manual_text}

    【良例・悪例データ】
    {good_examples}
    {bad_examples}

    【対象生徒の過去履歴（文脈の維持用）】
    {history_context}

    【本日の授業情報】
    - 単元・課題: {unit_info}
    
    【指示】
    成約事項やマニュアルに記載の内容を遵守し、アップロードされた授業の音声/動画データから、保護者用レポートの「生徒の様子」(「反応」「躓いた箇所」「成長した点」)項目を作ってください。
    """

    # 一時ファイルとして保存してアップロード (Streamlitの仕様対応)
    with st.spinner('データを解析中...'):
        try:
            # -------------------------------------------------------
            # ここが変更点: ローカル保存せず、メモリから直接アップロード
            # -------------------------------------------------------
            
            # StreamlitのファイルオブジェクトをBytesIOとして扱う
            # (media_fileは既にBytesIO互換ですが、念のためラップします)
            file_stream = io.BytesIO(media_file.getvalue())
            
            # Files APIへアップロード (メモリから直接)
            uploaded_content = client.files.upload(
                file=file_stream,
                config=dict(mime_type=media_file.type)
            )

            # 生成実行 (新しいSDKの書き方)
            response = client.models.generate_content(
                model="gemini-3-flash-preview",
                contents=[
                    uploaded_content, # アップロードしたファイルオブジェクト
                    prompt
                ]
            )
            return response.text
        
        except Exception as e:
            st.error(f"AI生成エラー: {e}")
            return None

# --- UI構築 ---

st.title("📝 講師用レポート自動生成ツール (PoC)")
st.markdown("授業の動画/音声をアップロードするだけで、保護者向けレポートの下書きを作成します。")

with st.spinner('学習データをスプレッドシートから取得中...'):
    manual_text, good_examples, bad_examples = load_learning_data()

# レイアウト: 左カラム（入力）、右カラム（出力）
col1, col2 = st.columns([1, 1])

with col1:
    st.subheader("1. 授業情報の入力")
    student_id = st.text_input("生徒番号", placeholder="例: 1120")
    
    uploaded_file = st.file_uploader("授業メディア (動画/音声)", type=['mp4', 'mp3', 'm4a', 'wav'])
    if uploaded_file:
        st.info(f"ファイルサイズ: {uploaded_file.size / 1024 / 1024:.2f} MB")
        if uploaded_file.size > 200 * 1024 * 1024:
            st.warning("⚠️ 200MBを超えています。処理に失敗する可能性があります。")

    unit_info = st.text_area("今日の設問", placeholder="Notionの問題・解説を直貼りしてください")

    generate_btn = st.button("レポートを生成する", type="primary", disabled=not uploaded_file)

with col2:
    st.subheader("2. 生成結果")

    if generate_btn and uploaded_file:
        # 1. 過去履歴取得
        history_context = get_student_history(student_id)
        
        # 2. 生成実行
        raw_text = generate_report(uploaded_file, student_id, unit_info, history_context, manual_text, good_examples, bad_examples)
        
        if raw_text:
            # 3. 整形処理
            final_text = clean_markdown(raw_text)
            
            st.success("生成完了！")
            
            # 出力表示 (コピー用)
            st.text_area("送付用テキスト (編集可)", value=final_text, height=400)
            
            # ワンクリックコピー用（st.codeを使うと右上にコピーボタンが出る仕様を利用）
            st.caption("以下のボックス右上のコピーボタンで全選択コピーできます")
            st.code(final_text, language="text")
            
    elif generate_btn and not uploaded_file:
        st.warning("メディアファイルをアップロードしてください。")

# フッター
st.markdown("---")
st.caption("Powered by Google Gemini 3 Flash | Dev: PoC Version 0.1")
