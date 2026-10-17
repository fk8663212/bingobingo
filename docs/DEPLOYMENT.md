# Oracle Cloud ARM 部署準備

這份部署設定尚未在目標 VPS 執行。先確認 OS、CPU／記憶體、目前反向代理與 80／443 使用情形、可用網域，再選擇下列步驟。現有網站不可直接被新 Caddy 覆蓋。

## 部署前需要的非機密資料

- OS 名稱與版本、`uname -m` 結果（預期 `aarch64`）、CPU／RAM／可用磁碟。
- 可使用的網域或子網域，DNS 是否能調整，是否已透過 Cloudflare 等代理。
- VPS 已裝 Docker／Compose 與否、目前有無 Nginx、Caddy、其他網站或占用連接埠。
- Oracle NSG／Security List、防火牆狀態、預期備份位置。

不需要提供登入密碼、SSH 私鑰或 Oracle 金鑰；使用者在自己的終端維持 SSH 登入。

## Linux + Docker Compose（未有反向代理時）

使用官方 Docker Engine 與 Compose plugin，安裝方式依目標 Linux 發行版確認後再執行。`python:3.13-slim-bookworm` 與 `caddy:2-alpine` 使用 ARM64 官方映像，應用本身沒有本機編譯相依套件。此 compose 使用 Linux host network；不適用未啟用 host network 的 macOS Docker Desktop。

1. 將專案放在例如 `/opt/bingobingo`，排除本機 `data/`、`.env`、`backups/`。DNS A 記錄指向 VPS 公開 IPv4，若有 AAAA 必須同樣能連入主機。
2. 在 Oracle NSG／Security List 及 OS 防火牆允許 TCP 80、443。SSH 維持原有安全設定；應用 8000 僅監聽 loopback，不對外開放。
3. 複製 `.env.example` 到 `.env`，設定 `BINGO_DOMAIN=bingo.example.com`、`ACME_EMAIL`，不需要填主持人密碼。

```sh
cd /opt/bingobingo
cp .env.example .env
# 用編輯器填入實際網域與信箱
docker compose config --quiet
docker compose build --pull app
docker compose run --rm app python manage.py init --title '導生晚宴' --tickets 120
# 在互動終端輸入並確認主持人密碼（至少 12 字元）
docker compose up -d
docker compose ps
docker compose logs --tail=50 app caddy
```

預設 data 與 backups 為 Docker named volumes，容器重建不刪資料。**勿使用 `docker compose down -v`**。Caddy 的憑證資料另存 named volume。

首次開啟 `https://你的網域/host` 登入，開放領卡。務必透過實際 HTTPS 網域列印專屬 QR 券；本機 127.0.0.1 QR 不能讓來賓手機連入。

若要先交叉建置檢查 ARM64：

```sh
docker buildx build --platform linux/arm64 --load -t dinner-bingo:arm64 .
```

目標 ARM VPS 上直接 `docker compose build` 即會建置原生架構。驗收後可將映像鎖到實際測過的 digest；活動前不臨時更新基底映像。

## HTTPS 與既有反向代理

Caddy 會依網域自動申請／更新公開憑證並將 HTTP 轉為 HTTPS。先確認 DNS、80／443 公網可達、系統時間正確。初次排錯可暫時用 DNS-only 記錄，若有 CDN／代理，另確認 SSE 無快取且連線可持續。

若 VPS 已使用 Caddy，保留既有服務，將 `deploy/Caddyfile` 的網域區段加入既有設定，只啟動 `docker compose up -d app`。不要再啟動第二個占用 80／443 的 Caddy。

若已有 Nginx，可沿用既有憑證與自動續期，在對應 HTTPS server 使用：

```nginx
location / {
    proxy_pass http://127.0.0.1:8000;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $remote_addr;
    proxy_set_header X-Forwarded-Proto https;
    proxy_set_header Connection "";
    proxy_buffering off;
    proxy_read_timeout 60s;
}
```

反向代理必須覆寫外部傳入的 X-Forwarded-For，且 app 僅能由 loopback 連入。正式 cookie 的 Secure 由 `BINGO_PUBLIC_URL=https://...` 啟用；Origin 與此設定必須相符。

## 備份與恢復

活動前、每一局結束及領獎更新後備份；活動期間可每 5 分鐘排程。使用 SQLite online backup，會得到一致快照；**不要在運行時只複製主 `.sqlite3` 而漏掉 WAL**。

```sh
cd /opt/bingobingo
sh scripts/backup.sh
```

腳本先在容器產生一致備份並檢查完整性，再複製到主機 `backups/`，權限 600。可在該部署使用者的 crontab 加入：

```cron
*/5 * * * * cd /opt/bingobingo && sh scripts/backup.sh >> /opt/bingobingo/backups/backup.log 2>&1
```

先手動執行一次建立 `backups/`。備份同時含主持人密碼雜湊、登入 session 及可恢復身分的領卡碼；請限制存取並將快照加密複製到另一台主機或物件儲存。`bingo_backups` volume 與主機資料夾都會累積快照，活動結束後依保留政策整理，不在腳本中自動刪除。

恢復前停止寫入、先保存現況；還原會回到備份當下，備份後的開獎／領獎需人工核對：

```sh
sh scripts/backup.sh
docker compose stop app
docker compose run --rm --no-deps --user root --entrypoint sh app -c 'mv /app/data/bingo.sqlite3 /app/data/bingo.before-restore.sqlite3; [ ! -f /app/data/bingo.sqlite3-wal ] || mv /app/data/bingo.sqlite3-wal /app/data/bingo.before-restore.sqlite3-wal; [ ! -f /app/data/bingo.sqlite3-shm ] || mv /app/data/bingo.sqlite3-shm /app/data/bingo.before-restore.sqlite3-shm'
# 先選定要還原的快照，將下行實際檔名替換後再執行
docker compose cp backups/SELECTED-SNAPSHOT.sqlite3 app:/app/data/bingo.sqlite3
docker compose run --rm --no-deps --user root --entrypoint sh app -c 'chown bingo:bingo /app/data/bingo.sqlite3; chmod 600 /app/data/bingo.sqlite3'
docker compose run --rm --no-deps app python -c "import sqlite3; db=sqlite3.connect('/app/data/bingo.sqlite3'); assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'; print('integrity OK')"
docker compose up -d app
```

`bingo.before-restore.*` 為保留的還原前副本；再次還原前，先將它們移到不同的備存位置，避免同名覆蓋。還原後核對目前回合、得獎名單、領獎時間，以及玩家原碼恢復。

修改主持人密碼：

```sh
docker compose exec app python manage.py password
```

會使所有舊主持人 session 失效，玩家登入不受影響。

## VPS 驗收

建置與實際 HTTPS 尚未在此機完成。上線前執行獨立負載演練，報告放在可寫入的 data volume：

```sh
docker compose exec app python scripts/load_test.py --players 150 --rounds 5 --output /app/data/load-test.json
docker compose cp app:/app/data/load-test.json ./backups/vps-load-test.json
```

此測試在容器內另開暫存 loopback server，確認 ARM 應用效能；另外須由場地網路實測正式 HTTPS 路徑，包含 Caddy、DNS 與 Wi-Fi。用真實 iPhone Safari 與 Android Chrome 掃未用 QR 碼，檢查 5×5 無橫向捲動、背景切換後同步、重新整理、關頁重開、飛航模式恢復、同碼換機恢復、投影全螢幕與領獎補登。

官方參考：[Docker 多平台建置](https://docs.docker.com/build/building/multi-platform/)、[Caddy 自動 HTTPS](https://caddyserver.com/docs/automatic-https)、[Caddy reverse_proxy](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy)、[SQLite backup API](https://docs.python.org/3.13/library/sqlite3.html#sqlite3.Connection.backup)。
