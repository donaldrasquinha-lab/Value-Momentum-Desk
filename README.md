# Value × Momentum Desk

Scans every NSE stock automatically for:

- **Cheap**: P/E below its industry median *and* below its own historical average P/E
- **Uptrend**: close > EMA50 > EMA200, with EMA50 rising
- **Start of a Supertrend**: Supertrend (10, 3) turned bullish within the last few bars
- **Growing ADX**: ADX above 20 and rising, with +DI above −DI
- **Momentum**: RSI 55–75, and price up at least 3% over 20 days

No broker login and no files to upload. GitHub Actions runs the scan every weekday at 4:15 PM IST, and also whenever you tap **Run scan now**. Results are published to a GitHub Pages site.

## Setup (one time, about 5 minutes)

1. Create a new GitHub repo, for example `Value-Momentum-Desk`. Upload every file in this folder, keeping `.github/workflows/scan.yml` in its folder.
2. Go to **Settings → Pages → Build and deployment → Source** and choose **GitHub Actions**.
3. Go to **Settings → Actions → General → Workflow permissions** and choose **Read and write**.
4. Open **Actions → Value Momentum Scan → Run workflow** to start the first scan.
   - The first run has to fetch fundamentals for about 2,000 stocks. That takes more than one 35-minute budget, so the first run or two will show partial P/E coverage.
   - After that, fundamentals refresh weekly and each daily run takes about 10–15 minutes.
5. Open `https://<you>.github.io/Value-Momentum-Desk/`.

### Run scan now button (optional)
1. Create a fine-grained token at GitHub → **Settings → Developer settings → Fine-grained tokens**.
2. Scope it to this repo only, with **Actions: Read and write**.
3. On the page, tap ⚙ and paste the token. It is stored only in that browser.

## How the numbers are calculated
- **Universe**: NSE `EQUITY_L.csv`, EQ and BE series.
- **P/E**: latest close ÷ trailing-12-month EPS from Yahoo Finance. Loss-makers have no P/E and are excluded.
- **Industry median**: median P/E of all scanned stocks in the same Yahoo industry, with at least 4 peers. If there are fewer peers, the sector median is used instead.
- **Historical avg P/E**: for each past fiscal year, average price during that year ÷ that year's EPS, then averaged across years. Up to about 4 years are available, and at least 2 are required.
- **Daily scan** (on the page): the rule thresholds filter live, so you can tune them without re-running the scan. Indicator periods are set as environment variables in `scan.py` (`EMA_FAST`, `ST_MULT` and similar).

## Notes
- Yahoo Finance sometimes rate-limits GitHub's servers. The script backs off and retries, and keeps cached fundamentals. If a run fails, just run it again.
- A low P/E against a high historical average can come from a one-off earnings jump. Check the earnings trend before acting on a value signal.
