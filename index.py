import streamlit as st
from google import genai # 新しいSDK
from google.genai import types
import mysql.connector
from mysql.connector import Error
from streamlit_gsheets import GSheetsConnection
import re
import io

# --- 初期設定 ---
st.set_page_config(page_title="授業レポート生成AI (PoC)", layout="wide")

# APIクライアント初期化 (新しいSDKの書き方)
if "gemini" in st.secrets:
    client = genai.Client(api_key=st.secrets["gemini"]["api_key"])
else:
    st.error("secrets.tomlにGemini APIキーが設定されていません。")
    st.stop()

@st.cache_data(ttl=600)  # 10分間キャッシュ (頻繁なAPI呼び出しを防ぐ)
def load_learning_data():
    """
    Google Spreadsheetから学習データ(マニュアル/良例/悪例)を読み込む
    各シートのA列などのテキストデータを結合して一つの文字列にする想定
    """
    try:
        conn = st.connection("gsheets", type=GSheetsConnection)
        
        # 1. マニュアルの読み込み
        df_manual = conn.read(worksheet="マニュアル", usecols=[0], header=None)
        manual_text = "\n".join(df_manual.dropna().astype(str).iloc[:, 0].tolist())

        # 2. 優良レポートの読み込み
        df_good = conn.read(worksheet="優良レポート", usecols=[0], header=None)
        good_examples = "\n".join(df_good.dropna().astype(str).iloc[:, 0].tolist())

        # 3. NGレポートの読み込み
        df_bad = conn.read(worksheet="NGレポート", usecols=[0], header=None)
        bad_examples = "\n".join(df_bad.dropna().astype(str).iloc[:, 0].tolist())
        
        return manual_text, good_examples, bad_examples

    except Exception as e:
        st.error(f"スプレッドシート読み込みエラー: {e}")
        # エラー時は空文字を返してアプリを止めない
        return "", "", ""

# --- 関数定義 ---

def get_db_connection():
    """MySQLへの接続を確立する"""
    try:
        connection = mysql.connector.connect(**st.secrets["mysql"])
        return connection
    except Error as e:
        st.error(f"データベース接続エラー: {e}")
        return None

def get_student_history(student_id):
    """
    生徒IDに基づいて過去のレポートを直近2件取得する。
    エラー時やデータなしの場合は「履歴なし」を返す。
    """
    history_text = "過去のレポート履歴はありません。"
    conn = get_db_connection()
    
    if conn and conn.is_connected():
        try:
            cursor = conn.cursor(dictionary=True)
            # 安全のためプレースホルダーを使用
            query = """
                SELECT content, created_at 
                FROM reports 
                WHERE student_id = %s 
                ORDER BY created_at DESC 
                LIMIT 2
            """
            cursor.execute(query, (student_id,))
            rows = cursor.fetchall()
            
            if rows:
                history_list = []
                for i, row in enumerate(rows):
                    date_str = row['created_at'].strftime('%Y-%m-%d')
                    history_list.append(f"--- 過去レポート({date_str}) ---\n{row['content']}")
                history_text = "\n\n".join(history_list)
                
        except Error as e:
            st.warning(f"履歴取得中にエラーが発生しました（処理は続行します）: {e}")
        finally:
            if conn.is_connected():
                cursor.close()
                conn.close()
    
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
    アップロードされた授業の音声/動画データから、生徒の「反応」「躓いた箇所」「成長した点」を分析し、
    本日の授業内容と組み合わせてレポートを執筆してください。
    特に、過去の履歴で指摘されていた課題が今回どうだったかについても触れてください。
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
                model="gemini-2.5-flash",
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
            st.text_area("LINE送付用テキスト (編集可)", value=final_text, height=400)
            
            # ワンクリックコピー用（st.codeを使うと右上にコピーボタンが出る仕様を利用）
            st.caption("以下のボックス右上のコピーボタンで全選択コピーできます")
            st.code(final_text, language="text")
            
    elif generate_btn and not uploaded_file:
        st.warning("メディアファイルをアップロードしてください。")

# フッター
st.markdown("---")
st.caption("Powered by Google Gemini 2.5 Flash | Dev: PoC Version 0.1")