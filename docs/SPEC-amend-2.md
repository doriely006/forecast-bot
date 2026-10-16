# forecast-bot 規格修正二（2026-10-16，Fable 第三輪審查後主 session 蓋章）
跟修正一衝突時以本檔為準。一併附加進 docs/SPEC.md 的 §12。

已驗證的賽制事實 [V]：Spring 正賽「每 2 小時開一題、每題只開 1.5 小時」；排名＝spot peer score 加總，沒交的題算 0；173 隻裡 65 隻是 Metaculus 自家 bot（metac-*）。
OpenRouter 10-16 實價：gpt-5.6-terra $2/$12（比 gpt-5.4 的 $2.5/$15 新又便宜）、gpt-5.6-sol $4/$20、gpt-5.5 $5/$30、gpt-5.6-luna $0.2/$1.2。

C1 排程改每 30 分：cron "17,47 * * * *"。前提是 B14（閒置輪 60 秒內結束）；README 月分鐘估算照 1,488 次重算。
C2 最終預測 model 預設 openai/gpt-5.6-terra，退回 openai/gpt-5.4；研究與解析維持 gpt-5.4-mini。正賽與 MiniBench 可各自指定 model（config）。
C3 key 的 limit 為 null 時不拒跑：改用 usage 配速，summary 標黃。402 照 B3 整輪停。README 的「設上限」改成「確認 OpenRouter 的 auto top-up 是關的」。
C4 取消「頭 48 小時只列題」：流程改成 Test Bot 跑 3 題過了就可以開 PUBLISH；summary 照樣印 open/close 時間當事後驗證。
C5 取消 B2 的跨輪持久化，只保留輪內重試。
C6 額度分配：第一輪 MiniBench 結算前（約 11-01）兩邊同一套配置，拚 MiniBench 高於平均換加碼；之後正賽拿大部分預算，MiniBench 用最便宜設定。比例與日期做成 config。
C7 standings 小工具提前到這一輪：讀正賽與 MiniBench 排行榜，算「非 metac-* bot 裡的名次」。沒有 token 無法實測，寫好介面與 fixture 測試。停損改成：正賽累計 40 題結算後，非 metac 名次落在後 50% 就停或換 model（README 與 BACKLOG 一起改）。
C8 README「只有你能做的事」補上：得獎後在通知 180 天內交 W-8BEN 與收款資料（逾期沒收，來源是 tournament-rules 的搜尋摘要，未讀到原文）、確認 auto top-up 關閉、賽季末 survey、每季重新申請額度。
C9 文件對帳：README 與 BACKLOG 裡每題成本、題數、月花費的數字全部對齊配速版本，舊數字刪掉。
C10 spec 加一句：正賽與 MiniBench 是兩個獨立排行榜，MiniBench 用便宜設定只影響加碼機會，不影響正賽分數。
