# 權證合理價 Streamlit

Streamlit 版本的權證盯盤工具。輸入權證代號後會自動抓上市 / 上櫃權證資料、即時報價、標的股價與 Yuanta / warrantwin 合理價。

## 啟動

```bash
pip install -r requirements.txt
streamlit run app.py
```

預設開啟 `http://127.0.0.1:8501`。

## 功能

- 新增、儲存、刪除權證。
- 手動上移 / 下移排序。
- 一鍵更新全部價格。
- 顯示合理價、權證報價、現貨股價。
- 輸入股價試算權證價格。
- 輸入權證價格反推股價。

本地清單儲存在 `saved_warrants.json`，此檔案不會被 Git 追蹤。
