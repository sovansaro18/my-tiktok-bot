# របៀបដំណើរការ Telegram Downloader Bot លើ PC (Windows)

> គោលបំណង៖ ឲ្យបងអាចដំណើរការ bot ជាបណ្ដោះអាសន្នលើ PC ដោយមិនពឹង Render។

---

## 1) ត្រូវតំឡើងអ្វីខ្លះ

### ✅ Python
- Python **3.10 ឬ 3.11** (64-bit)
- ពេល install សូមធីក **Add Python to PATH**

ពិនិត្យ (CMD/PowerShell):
```
python --version
pip --version
```

### ✅ FFmpeg
FFmpeg ត្រូវការ​សម្រាប់ extraction/post-processing (ពិសេស audio)។

ពិនិត្យ (CMD/PowerShell):
```
ffmpeg -version
```

> បើមិនមាន FFmpeg, វីដេអូខ្លះអាចទាញបាន តែ audio conversion ឬ merge អាច fail។

---

## 2) រៀបចំ project
1) Extract ZIP
2) នៅក្នុង folder project នេះ មាន scripts:
- `setup_windows.bat` (តំឡើង dependencies + បង្កើត venv)
- `run_windows.bat` (ដំណើរការ bot)

---

## 3) បង្កើត `.env`
មាន `.env.example` រួចហើយ។ បងធ្វើ:
- copy `.env.example` → `.env`
- បញ្ចូល values:

```
BOT_TOKEN=xxxxx:yyyyy
ADMIN_ID=123456789
LOG_CHANNEL_ID=-1003569125986
REPORT_CHANNEL_ID=-1003569125986

# optional
MONGO_URI=mongodb+srv://...
PORT=10000
COOKIES_FILE=cookies.txt
```

### Cookies (optional)
- សម្រាប់ YouTube age-restricted / Facebook login-required → ត្រូវ `cookies.txt`
- ងាយៗ៖ ដាក់ `cookies.txt` នៅ root project ហើយ set `COOKIES_FILE=cookies.txt`

---

## 4) ដំណើរការ (មិនចាំបាច់ចេះ command ច្រើន)
1) Double click `setup_windows.bat` (ធ្វើតែ 1 ដង)
2) Double click `run_windows.bat`

---

## 5) Troubleshooting

### A) `BOT_TOKEN/ADMIN_ID missing`
- `.env` មិនមាន ឬ key ខុសឈ្មោះ
- សូមពិនិត្យ `.env` នៅ root folder

### B) `TelegramConflictError (getUpdates conflict)`
- មាន bot instance ផ្សេងកំពុងរត់ (Render/PC/Server) ជាមួយ token ដូចគ្នា
- ដោះស្រាយ៖ បិទ instance ផ្សេង ឬ reset token ក្នុង BotFather

### C) `ffmpeg not found`
- តំឡើង FFmpeg ហើយ add to PATH

### D) YouTube: `Age-restricted. Need cookies.txt`
- cookies មិនមាន/expire/មិន logged-in
- export cookies.txt ថ្មី ហើយ set `COOKIES_FILE`

---

បើបងចង់ឲ្យ bot run ពេល restart PC, ខ្ញុំអាចណែនាំជំហានបន្ថែម (Task Scheduler / NSSM)។
