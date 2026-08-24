# مدير ADB اللاسلكي

[![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20Linux%20%7C%20macOS-blue)](#التثبيت)
[![Python](https://img.shields.io/badge/python-3.9%2B-green)](requirements.txt)
[![Tests](https://img.shields.io/badge/tests-38%2F38%20passing-brightgreen)](tests/run_tests.sh)
[![CI](https://github.com/ALSRKAL/adb-wireless-manager/actions/workflows/ci.yml/badge.svg)](https://github.com/ALSRKAL/adb-wireless-manager/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-yellow)](LICENSE)

**English documentation | [English README](README.md)**

ودّع الكابل! اتصل بأجهزة أندرويد عبر Wi-Fi بنقرة واحدة — من أيقونة شريط
المهام أو من سطر الأوامر. يدعم **عدة هواتف في نفس الوقت**، يصمد أمام انقطاع
الشبكة وإعادة تشغيل اللابتوب وحتى إعادة تشغيل الهاتف (اكتشاف تلقائي عبر mDNS)،
ويري شاشة الهاتف على الكمبيوتر عبر scrcpy.

---

## ✨ المميزات

| | الميزة | الأيقونة (GUI) | سطر الأوامر |
|--|--------|:---:|:---:|
| 📡 | اتصال لاسلكي أول مرة عبر USB | ✅ | ✅ |
| 🔁 | إعادة اتصال تلقائية للأجهزة المحفوظة (ذاكرة + mDNS) | ✅ | ✅ |
| 👀 | وضع المراقبة — حلقة إصلاح ذاتي للاتصال | systemd | `watch` |
| 📱 | دعم عدة أجهزة معًا | ✅ | ✅ |
| 🖥️ | عرض الشاشة عبر scrcpy لكل جهاز | ✅ | — |
| 📴 | الأجهزة المفصولة تبقى ظاهرة مع زر إعادة اتصال خاص بها | ✅ | — |
| 🚫 | تعليق يدوي: الجهاز الذي تفصله بنفسك يبقى مفصولًا حتى تعيده أنت (المراقبة التلقائية تتجاهله) | ✅ | ✅ |
| 🩺 | فحص البيئة (adb / scrcpy / mDNS / الشبكة) | ✅ | ✅ |
| 🟢 | أيقونة حية (أخضر = متصل، أحمر = لا شيء، أصفر = جارٍ العمل) | ✅ | — |
| ⚙️ | نافذة إعدادات كاملة (اللغة، مدة الفحص، خيارات scrcpy، المنافذ) + ملف `settings.json` موحد | ✅ | يقرؤه |
| 📸 | لقطة شاشة وتسجيل شاشة 30 ثانية لكل جهاز → تُحفظ على سطح المكتب | ✅ | — |
| 📦 | تثبيت APK بمنطقة إفلات عائمة (أو منتقي ملفات) | ✅ | — |
| ℹ️ | بطاقة معلومات الجهاز: البطارية، التخزين، نسخة أندرويد، عناوين IP | ✅ | — |
| ✏️ | أسماء مستعارة للأجهزة تظهر في كل مكان | ✅ | — |
| 🌗 | أيقونة تتكيف مع الثيم (داكن/فاتح) + 🔔 إشعارات بأزرار فعل | ✅ | — |
| 🔗 | معالج اقتران لاسلكي موجّه (أندرويد 11+ بدون كابل) مع لوحة فحص جاهزية حيّة (خيارات المطوّر / USB debugging / Wireless debugging) وتوليد QR لبيانات الاقتران | ✅ | ✅ |
| 🤖 | CI على Windows/Linux/macOS + بناء تلقائي لملفات EXE والتنفيذيات في [Releases](https://github.com/ALSRKAL/adb-wireless-manager/releases) | — | — |
| 🔄 | فاحص تحديثات مدمج يقارن مع إصدارات GitHub | ✅ | — |

## 🗂 هيكل المشروع

```
adb-wireless-manager/
├── scripts/
│   ├── adbconnect.sh        سكربت CLI لأنظمة Linux/macOS/BSD
│   └── adbconnect.ps1       سكربت CLI لويندوز (PowerShell 5+)
├── tray/
│   └── adbtray.py           تطبيق أيقونة شريط المهام (Qt) متعدد المنصات
├── tests/
│   ├── test_core.py         اختبارات الوحدة (38 حالة، بدون الحاجة لجهاز)
│   └── run_tests.sh         مشغّل الاختبارات الكامل (LIVE=1 للفحص الحي)
├── install.sh               مثبّت Linux/macOS (--with-watch)
├── uninstall.sh             مزيل التثبيت
├── install.ps1              مثبّت ويندوز
└── requirements.txt         متطلبات بايثون
```

## 📋 المتطلبات

| المتطلب | إلزامي؟ | ملاحظات |
|---------|:-------:|---------|
| هاتف أندرويد مع تفعيل USB debugging | ✅ | خيارات المطوّر ← تصحيح USB |
| أداة `adb` | ✅ | ويندوز: `winget install Google.PlatformTools` · أوبنتو: `sudo apt install adb` · ماك: `brew install android-platform-tools` |
| بايثون **3.9+** + **PyQt5** | ✅ للأيقونة | مستخدمو سطر الأوامر فقط يمكنهم تجاوزها |
| `scrcpy` | اختياري | عرض الشاشة · `winget install scrcpy` / `sudo apt install scrcpy` |
| GNOME (لينكس) | اختياري | أيقونة الشريط تحتاج تفعيل AppIndicators: `gnome-extensions enable ubuntu-appindicators@ubuntu.com` |

اعتماديات بايثون (`pip install -r requirements.txt`):

```
PyQt5>=5.15
```

## 🚀 التثبيت

### لينكس / ماك

```bash
git clone https://github.com/ALSRKAL/adb-wireless-manager.git
cd adb-wireless-manager

./install.sh                 # أيقونة فقط
./install.sh --with-watch    # الأيقونة + خدمة إعادة الاتصال التلقائي
```

### ويندوز (بوويرشل)

```powershell
git clone https://github.com/ALSRKAL/adb-wireless-manager.git
cd adb-wireless-manager
powershell -ExecutionPolicy Bypass -File install.ps1
```

يتحقق المثبّت من adb وscrcpy وPyQt5، ويسجّل الأيقونة لتعمل مع بدء الجلسة،
و(مع `--with-watch`) ينشئ خدمة systemd تراقب وتعيد الاتصال بالخلفية.

## 🕹 الاستخدام

### أول مرة مع هاتف جديد (تحتاج الكابل مرة واحدة)

1. وصّل الهاتف بـ USB واقبل نافذة التصحيح على شاشته.
2. اضغط أيقونة الشريط ← **🔌 توصيل جهاز عبر USB** — أو نفّذ:

```bash
# لينكس / ماك                       # ويندوز (بوويرشل)
./scripts/adbconnect.sh connect     powershell -File scripts\adbconnect.ps1 connect
```

3. اسحب الكابل. تم — الهاتف محفوظ للأبد.

### بعدها كل يوم — بدون كابل

- **الأيقونة:** اضغط عليها؛ ستظهر كل الهواتف المحفوظة. استخدم
  **🔄 إعادة اتصال الكل**، أو افتح عنصر 📴 الخاص بهاتف غير متصل واضغط
  **إعادة الاتصال الآن**. اضغط هاتفًا متصلًا 📱 ← **عرض الشاشة (scrcpy)**.
- **سطر الأوامر:**

```bash
./scripts/adbconnect.sh reconnect   # ذاكرة الأجهزة + اكتشاف mDNS
./scripts/adbconnect.sh watch       # مراقبة وإصلاح الاتصال باستمرار
./scripts/adbconnect.sh list        # عرض الأجهزة
./scripts/adbconnect.sh disconnect  # فصل كل الاتصالات اللاسلكية
./scripts/adbconnect.sh doctor      # تشخيص كامل للبيئة
./scripts/adbconnect.sh pair        # اقتران لاسلكي لأندرويد 11+
```

```powershell
powershell -File scripts\adbconnect.ps1 reconnect
powershell -File scripts\adbconnect.ps1 doctor
```

### ما مدى "الدائمة" هذا الاتصال؟

| الحدث | ماذا يحدث |
|-------|-----------|
| انقطعت الشبكة ورجعت | خدمة المراقبة / الأيقونة تعيد الاتصال تلقائيًا |
| أعدت تشغيل اللابتوب | الأيقونة (+ خدمة المراقبة) تتصل عند تسجيل الدخول |
| أعدت تشغيل الهاتف | منفذه اللاسلكي يتغير ← يُكتشف مجددًا عبر mDNS |
| ألغيت الاقتران من الهاتف | نفّذ `pair` أو وصّل الكابل مرة واحدة |

## ⚙️ الإعدادات

سكربت Bash يقرأ الإعدادات من `~/.config/adbconnect/config`
(`KEY=VALUE` مثل `AUTO_SCRCPY=false` و`START_PORT=5555`). الأجهزة المحفوظة
في `~/.local/share/adbconnect/devices.tsv` (وعلى ويندوز `%APPDATA%\adbconnect`).

## 🧪 الاختبارات

```bash
./tests/run_tests.sh          # تحليل ثابت + 15 اختبار وحدة
LIVE=1 ./tests/run_tests.sh   # + فحص حي لـ adb والخدمة والأيقونة
```

## 🛠 حل المشاكل

| المشكلة | الحل |
|---------|------|
| رسالة unauthorized | افتح قفل الهاتف واقبل نافذة التخويل |
| أيقونة الشريط لا تظهر (GNOME) | فعّل AppIndicators ثم سجّل الخروج والدخول |
| فشلت إعادة الاتصال بعد إعادة تشغيل الهاتف مباشرة | استخدم أمر `pair` أو وصّل الكابل مرة واحدة |
| المنفذ مستخدم | الأداة تكمل تلقائيًا للمنفذ التالي (5555…5699) |
| اقتران QR / الاكتشاف التلقائي لا يرى الهاتف إطلاقًا | نسخ adb من مستودعات التوزيعات كثيرًا ما تُبنى بدون mDNS. المثبّت يحمّل تلقائيًا platform-tools الرسمية إلى `~/.local/share/awm/` ويستخدمها (تحقق بـ `adb mdns check`) |
| نافذة scrcpy لا تفتح | ثبّت scrcpy أو عطّل التشغيل التلقائي (`AUTO_SCRCPY=false`) |

## 🤝 المساهمة

التعديلات مرحب بها! رجاءً شغّل `./tests/run_tests.sh` قبل أي طلب دمج.

## 📄 الترخيص

[MIT](LICENSE) © محمد الصرقلي ([@ALSRKAL](https://github.com/ALSRKAL))
