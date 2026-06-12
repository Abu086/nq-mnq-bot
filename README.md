# 📈 NQ / MNQ Bull Call Spread — Paper Trading Bot

Automated paper trading bot for **E-mini Nasdaq-100 (NQ)** and **Micro E-mini Nasdaq-100 (MNQ)** futures options.  
Runs **daily at 10:00 AM ET** via GitHub Actions — no computer needed.

---

## 🗂️ Project Structure

```
├── trading_bot.py              # Main bot logic
├── trading_journal.xlsx        # Auto-updated Excel journal
├── requirements.txt            # Python dependencies
├── logs/
│   └── bot.log                 # Daily run logs
└── .github/
    └── workflows/
        └── run_bot.yml         # GitHub Actions scheduler
```

---

## 💰 Strategy: Bull Call Spread

| Parameter       | NQ                        | MNQ                       |
|-----------------|---------------------------|---------------------------|
| Multiplier      | $20 × index               | $2 × index                |
| Strike Width    | 25 points                 | 25 points                 |
| Contracts       | 1 spread                  | 3 spreads                 |
| Budget          | $1,000 (paper)            | $1,000 (paper)            |
| Entry Time      | 10:00 AM ET               | 10:00 AM ET               |
| Target          | 30% of max profit         | 30% of max profit         |
| Stop Loss       | 30% of premium paid       | 30% of premium paid       |

---

## 💸 Fees Deducted Per Trade (Round-Trip)

| Fee Type        | NQ (per contract) | MNQ (per contract) |
|-----------------|-------------------|--------------------|
| CME Exchange    | $1.18             | $0.30              |
| Broker Comm.    | $2.50             | $0.50              |
| NFA Regulatory  | $0.02             | $0.02              |
| **Total/leg**   | **$3.70**         | **$0.82**          |
| **Round-trip**  | **$7.40**         | **$1.64**          |

All fees are automatically deducted from P&L and recorded in the journal.

---

## 📊 Excel Journal Sheets

| Sheet            | Contents                                      |
|------------------|-----------------------------------------------|
| `NQ_Trades`      | All NQ spread trades with full detail         |
| `MNQ_Trades`     | All MNQ spread trades with full detail        |
| `US_Stocks`      | US stock trades (add manually or extend bot)  |
| `Fee_Breakdown`  | Per-trade fee detail for all instruments      |
| `Summary`        | Live P&L, win rate, return on budget          |

---

## 🚀 Setup (One Time)

### 1. Fork / Clone this repo to your GitHub account

### 2. Enable GitHub Actions
- Go to your repo → **Actions** tab → Enable workflows

### 3. That's it!
The bot runs automatically **Monday–Friday at 10:00 AM ET**.  
The journal (`trading_journal.xlsx`) is committed back to your repo after each run.

### Manual Run
Go to **Actions → NQ/MNQ Paper Trading Bot → Run workflow** to trigger manually.

---

## 📥 Download Journal
After each run, download `trading_journal.xlsx` from your repo to view in Excel or Google Sheets.

---

## ⚠️ Disclaimer
This is a **paper trading simulation only**. No real money is used or at risk.  
Not financial advice. Always consult a licensed advisor before live trading.
