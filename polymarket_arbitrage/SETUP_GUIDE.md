# Polymarket Arbitrage Bot — 버지니아 서버 배포 가이드

## 목차

1. [사전 준비](#1-사전-준비)
2. [Polymarket 계정 및 지갑 설정](#2-polymarket-계정-및-지갑-설정)
3. [서버 환경 세팅](#3-서버-환경-세팅)
4. [봇 코드 배포](#4-봇-코드-배포)
5. [설정 파일 구성](#5-설정-파일-구성)
6. [Dry Run (모의 실행)](#6-dry-run-모의-실행)
7. [Live 실행](#7-live-실행)
8. [모니터링 및 관리](#8-모니터링-및-관리)
9. [문제 해결](#9-문제-해결)

---

## 1. 사전 준비

### 필요한 것

| 항목 | 설명 |
|------|------|
| 버지니아 서버 | AWS us-east-1 또는 유사한 US-East 서버 (Ubuntu 20.04+ 권장) |
| Python 3.10+ | 서버에 설치 필요 |
| USDC (Polygon) | 봇 운용 자금. 테스트는 $100~500, 본격 운용은 $1000+ 권장 |
| MetaMask 또는 EOA 지갑 | Polygon 네트워크 지원하는 지갑 |
| MATIC (소량) | Polygon 가스비용. $1~5 정도면 충분 |

### 중요 주의사항

> **Polymarket은 한국 IP를 차단합니다.**
> - 웹사이트 접속 및 계정 생성은 VPN이 필요할 수 있습니다
> - 하지만 **버지니아 서버에서 API 호출**은 문제없습니다 (US IP이므로)
> - 봇은 반드시 **버지니아 서버에서** 실행하세요

---

## 2. Polymarket 계정 및 지갑 설정

### Step 2-1: Polygon 지갑 생성

이미 MetaMask 등의 지갑이 있다면 건너뛰세요.

```bash
# 새 지갑을 프로그래밍으로 생성하려면:
python3 -c "
from eth_account import Account
acct = Account.create()
print(f'Address:     {acct.address}')
print(f'Private Key: {acct.key.hex()}')
print()
print('⚠️  Private Key를 안전하게 보관하세요!')
print('⚠️  절대 다른 사람에게 공유하지 마세요!')
"
```

또는 MetaMask에서:
1. MetaMask 설치 → 새 지갑 생성
2. 네트워크 → Polygon Mainnet 추가
3. 계정 설정 → Private Key 내보내기 (나중에 필요)

### Step 2-2: USDC 입금 (Polygon 네트워크)

봇이 거래하려면 **Polygon 네트워크의 USDC**가 필요합니다.

**방법 A: 거래소에서 직접 출금**
1. 바이낸스/업비트 등에서 USDC 구매
2. 출금 네트워크로 **Polygon** 선택
3. 위에서 만든 지갑 주소로 출금

**방법 B: 브릿지 사용**
1. Ethereum에 USDC가 있다면 → [Polygon Bridge](https://wallet.polygon.technology/bridge) 사용
2. Ethereum USDC → Polygon USDC로 브릿지

**방법 C: Polymarket에 직접 입금**
1. Polymarket 웹사이트 접속 (VPN 필요할 수 있음)
2. Deposit 버튼 → 원하는 방법으로 입금
3. Polymarket이 자동으로 Polygon USDC로 변환

> **가스비**: Polygon에서 MATIC이 소량 필요합니다 (트랜잭션 서명용).
> 바이낸스에서 MATIC을 $2~5 정도 Polygon 네트워크로 출금하세요.

### Step 2-3: Polymarket CLOB 승인

Polymarket에서 처음 거래하려면 CLOB(Central Limit Order Book)에 대한
USDC 지출 승인이 필요합니다.

```bash
# 서버에서 한 번만 실행 (봇 설정 후)
python3 -c "
from py_clob_client.client import ClobClient

client = ClobClient(
    'https://clob.polymarket.com',
    key='YOUR_PRIVATE_KEY_HERE',
    chain_id=137,
    signature_type=0,
    funder='YOUR_WALLET_ADDRESS_HERE',
)

# API credentials 생성 (처음 한 번)
creds = client.create_or_derive_api_creds()
client.set_api_creds(creds)
print(f'API Key: {creds.api_key}')
print(f'API Secret: {creds.api_secret}')
print(f'Passphrase: {creds.api_passphrase}')

# USDC 잔액 확인
print(f'Allowances: {client.get_balance_allowance()}')
"
```

---

## 3. 서버 환경 세팅

### Step 3-1: 서버 접속

```bash
ssh user@your-virginia-server-ip
```

### Step 3-2: Python 3.10+ 설치

```bash
# Ubuntu/Debian
sudo apt update
sudo apt install -y python3.10 python3.10-venv python3-pip git

# 버전 확인
python3 --version  # 3.10 이상이어야 함
```

### Step 3-3: 가상환경 생성

```bash
mkdir -p ~/polymarket-bot
cd ~/polymarket-bot

# 가상환경 생성 및 활성화
python3 -m venv venv
source venv/bin/activate
```

---

## 4. 봇 코드 배포

### Step 4-1: 코드 다운로드

```bash
cd ~/polymarket-bot

# Git으로 클론 (저장소가 있는 경우)
git clone YOUR_REPO_URL polymarket_arbitrage

# 또는 파일을 직접 복사
# scp -r /local/path/polymarket_arbitrage/ user@server:~/polymarket-bot/
```

### Step 4-2: 의존성 설치

```bash
cd ~/polymarket-bot
source venv/bin/activate

pip install --upgrade pip
pip install -r polymarket_arbitrage/requirements.txt
```

설치 확인:
```bash
python3 -c "
from py_clob_client.client import ClobClient
import websockets
import requests
print('✅ 모든 의존성 설치 완료')
"
```

---

## 5. 설정 파일 구성

### Step 5-1: .env 파일 생성

```bash
cd ~/polymarket-bot/polymarket_arbitrage
cp .env.example .env
nano .env   # 또는 vim .env
```

### Step 5-2: .env 내용 수정

```env
# ===== 필수 설정 =====

# 지갑 Private Key (0x로 시작하는 64자리 hex)
PRIVATE_KEY=0xabcdef1234567890...

# 지갑 주소 (0x로 시작하는 40자리 hex)
WALLET_ADDRESS=0x1234567890abcdef...

# ===== 거래 파라미터 =====

# 한 번의 아비트라지에 투입할 최대 금액 (USD)
# 처음에는 작게 시작하세요!
MAX_BET_SIZE=20.0

# 최소 수익 마진 (1 cent = 0.01)
# 낮을수록 더 많은 기회를 잡지만, 수수료에 먹힐 수 있음
MIN_PROFIT_MARGIN=0.01

# 뱅크롤 대비 1회 최대 비율 (5% = 0.05)
MAX_BANKROLL_FRACTION=0.05

# ===== 마켓 설정 =====

# 거래할 코인 (BTC가 89%로 가장 기회가 많음)
ASSETS=btc,eth,sol,xrp

# 마켓 시간 단위
DURATIONS=5m,15m

# ===== 안전 설정 =====

# true = 모의 실행 (실제 거래 안 함), false = 실제 거래
DRY_RUN=true
```

### Step 5-3: 권한 설정

```bash
# .env 파일은 본인만 읽을 수 있게
chmod 600 .env
```

---

## 6. Dry Run (모의 실행)

**실제 돈을 넣기 전에 반드시 Dry Run으로 먼저 테스트하세요!**

### Step 6-1: 현재 마켓 스캔

```bash
cd ~/polymarket-bot/polymarket_arbitrage
source ../venv/bin/activate

# 현재 활성 마켓 확인
python3 main.py scan
```

이 결과에서 활성 마켓이 보이면 API 연결이 정상입니다.

### Step 6-2: Dry Run (Polling 모드)

```bash
# DRY_RUN=true 상태에서 실행
python3 main.py run --dry-run
```

로그에 아비트라지 기회 감지 및 "[DRY RUN]" 가상 거래가 보이면 정상입니다.
`Ctrl+C`로 종료.

### Step 6-3: Dry Run (WebSocket 모드)

```bash
# WebSocket 모드로 실행 (더 빠름)
python3 main.py run --dry-run --ws
```

WebSocket 모드에서:
- "Registered X markets for WebSocket monitoring" 메시지가 나와야 함
- "WS OPPORTUNITY:" 메시지가 나오면 아비트라지 감지 성공

### Step 6-4: 백테스팅으로 수익성 확인

```bash
# 버지니아 서버에서 1시간 백테스트
python3 backtester.py --mode virginia_ws --hours 1 --seed 42

# 위치별 비교 (한국 vs 버지니아 vs 코로케이션)
python3 backtester.py --compare-locations --hours 2

# 전체 모드 비교 (폴링 포함)
python3 backtester.py --compare --hours 2
```

---

## 7. Live 실행

### Step 7-1: 소액으로 시작

> **중요**: 처음에는 반드시 소액($50~100)으로 시작하세요!
> 정상 작동이 확인되면 점차 금액을 늘리세요.

```bash
# .env 파일 수정
nano .env
```

변경:
```env
DRY_RUN=false
MAX_BET_SIZE=10.0        # 처음에는 1회 $10로 시작
MAX_BANKROLL_FRACTION=0.05
```

### Step 7-2: WebSocket 모드로 실행

```bash
cd ~/polymarket-bot/polymarket_arbitrage
source ../venv/bin/activate

# Live 실행 (WebSocket)
python3 main.py run --live --ws
```

### Step 7-3: 백그라운드 실행 (screen/tmux)

봇을 24시간 돌리려면 터미널이 닫혀도 유지되어야 합니다.

```bash
# 방법 1: screen 사용
screen -S polymarket-bot
cd ~/polymarket-bot/polymarket_arbitrage
source ../venv/bin/activate
python3 main.py run --live --ws

# screen에서 나가기: Ctrl+A, D
# 다시 들어가기: screen -r polymarket-bot
```

```bash
# 방법 2: tmux 사용
tmux new -s polymarket-bot
cd ~/polymarket-bot/polymarket_arbitrage
source ../venv/bin/activate
python3 main.py run --live --ws

# tmux에서 나가기: Ctrl+B, D
# 다시 들어가기: tmux attach -t polymarket-bot
```

```bash
# 방법 3: systemd 서비스 (가장 안정적, 자동 재시작)
sudo tee /etc/systemd/system/polymarket-bot.service << 'EOF'
[Unit]
Description=Polymarket Arbitrage Bot
After=network.target

[Service]
Type=simple
User=YOUR_USERNAME
WorkingDirectory=/home/YOUR_USERNAME/polymarket-bot/polymarket_arbitrage
ExecStart=/home/YOUR_USERNAME/polymarket-bot/venv/bin/python3 main.py run --live --ws
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable polymarket-bot
sudo systemctl start polymarket-bot

# 상태 확인
sudo systemctl status polymarket-bot

# 로그 보기
sudo journalctl -u polymarket-bot -f
```

---

## 8. 모니터링 및 관리

### 로그 확인

```bash
# systemd 서비스 로그
sudo journalctl -u polymarket-bot -f --since "10 minutes ago"

# screen/tmux라면 해당 세션에 재접속
screen -r polymarket-bot
# 또는
tmux attach -t polymarket-bot
```

### 지갑 잔액 확인

```bash
cd ~/polymarket-bot/polymarket_arbitrage
source ../venv/bin/activate

python3 -c "
from py_clob_client.client import ClobClient
from config import CLOB_API_URL, PRIVATE_KEY, CHAIN_ID, SIGNATURE_TYPE, WALLET_ADDRESS

client = ClobClient(
    CLOB_API_URL, key=PRIVATE_KEY,
    chain_id=CHAIN_ID, signature_type=SIGNATURE_TYPE,
    funder=WALLET_ADDRESS,
)
client.set_api_creds(client.create_or_derive_api_creds())
print(client.get_balance_allowance())
"
```

### 타겟 계정 분석 (참고용)

```bash
# 타겟 계정이 어떻게 거래하는지 확인
python3 main.py analyze

# 상세 분석 (maker/taker 비율 등)
python3 main.py analyze --deep
```

### 봇 중지

```bash
# screen/tmux: 해당 세션에서 Ctrl+C
# systemd:
sudo systemctl stop polymarket-bot
```

---

## 9. 문제 해결

### "Missing PRIVATE_KEY or WALLET_ADDRESS"

→ `.env` 파일이 올바른 위치에 있는지 확인. 봇을 `polymarket_arbitrage/` 디렉토리에서 실행하세요.

### "Failed to initialize CLOB client"

→ Private Key 형식 확인 (0x 포함 66자리). Polygon 네트워크 USDC 잔액 확인.

### "No active markets found"

→ 정상일 수 있습니다. 크립토 Up/Down 마켓은 5분/15분 단위로 열리고 닫히므로, 마켓이 열릴 때까지 기다리세요.

### WebSocket 연결 끊김

→ 봇이 자동으로 재연결합니다. 네트워크가 불안정하면 polling 모드(`--ws` 없이)로 먼저 테스트하세요.

### Cloudflare 403 에러

→ Polymarket은 Cloudflare WAF를 사용합니다. 너무 빠른 요청은 차단될 수 있습니다. 서버 IP가 차단됐다면 잠시 기다리거나 IP를 변경하세요.

### 한국에서 접속 불가

→ **봇은 반드시 버지니아 서버에서** 실행하세요. 한국 IP는 Polymarket에서 차단됩니다. SSH로 버지니아 서버에 접속해서 실행하면 됩니다.

---

## 권장 설정 요약

| 단계 | MAX_BET_SIZE | DRY_RUN | 모드 | 기간 |
|------|-------------|---------|------|------|
| 1. 테스트 | $10 | true | `--dry-run --ws` | 1-2시간 |
| 2. 소액 라이브 | $10 | false | `--live --ws` | 1일 |
| 3. 검증 후 | $20-50 | false | `--live --ws` | 1주 |
| 4. 본격 운용 | $50+ | false | `--live --ws` | 상시 |

> **핵심**: 항상 WebSocket 모드(`--ws`)를 사용하세요. Polling 대비 7~8배 더 많은 기회를 잡습니다.
