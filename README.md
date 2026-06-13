# 權證合理價 Streamlit

Streamlit 版本的權證盯盤工具。輸入權證代號後會自動抓上市 / 上櫃權證資料、即時報價、標的股價與券商官方合理價。

## 啟動

```bash
pip install -r requirements.txt
streamlit run app.py
```

預設開啟 `http://127.0.0.1:8501`。

## 功能

- 新增、儲存、刪除權證，清單只儲存在 Supabase。
- 手動上移 / 下移排序。
- 一鍵更新全部價格。
- 顯示合理價、權證報價、現貨股價。
- 輸入股價試算權證價格。
- 輸入權證價格反推股價。
- 元大權證只讀元大合理價；凱基權證只讀凱基理論價。券商資料讀不到時會顯示錯誤，不會改用另一家資料混算。

## Supabase 儲存

清單儲存必須設定 Supabase。程式不再讀寫本機 `saved_warrants.json`，避免 Streamlit Cloud 重新整理後把已刪除的權證又從 repo 檔案帶回來。

建立資料表：

```sql
create table if not exists warrant_watch_lists (
  profile_id text primary key,
  items jsonb not null default '[]'::jsonb,
  updated_at timestamptz not null default now()
);
```

Streamlit secrets：

```toml
SUPABASE_URL = "https://你的專案.supabase.co"
SUPABASE_KEY = "你的 anon 或 service role key"
SUPABASE_TABLE = "warrant_watch_lists"
SUPABASE_PROFILE_ID = "default"
```

也支援巢狀 secrets：

```toml
[supabase]
url = "https://你的專案.supabase.co"
key = "你的 anon 或 service role key"
table = "warrant_watch_lists"
profile_id = "default"
```

沒有設定 Supabase 時，畫面會顯示 Supabase 錯誤，清單不會寫到本機。
