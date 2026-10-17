# Verification — 2026-09-19 · منطق إعادة تنزيل YouTube + ضغط الواجهة

## Actions acceptance follow-up — 2026-10-17

The first managed-account push passed the 431-test suite, real OpenCode account isolation/failover, native account UI and Docker/PostgreSQL gates. PWA acceptance exposed a test fixture that replaced a fixed old Service Worker version; the fixture now substitutes the declared version and asserts that it matched. The real HTTPS PWA acceptance passed locally with the new public-cache version. TLS acceptance also exposed a shutdown race between a completed connector thread and the server's WebSocket `connection_lost` callback. Cleanup now waits for server transports to drain, and a relay-thread exception fails acceptance explicitly. The complete two-desktop TLS/browser acceptance passed against both Uvicorn 0.52.4 and the Actions version 0.54.0, without a shutdown traceback. A subsequent native account run completed its DOCX but exposed an audit-read race: the fixture now waits for the scheduler to release its active worker before asserting the finalized attempt outcomes. These changes affect verification only.

## Managed ChatGPT accounts — 2026-10-17

- `python -m pytest tests -q --tb=short`: **431 passed**. The account tests cover three-account routing, shared per-account capacity, explicit preference, sticky selection, account/model quota scope, conservative 429 classification, unknown-reset probes, persistent cooldowns, stale parallel results, duplicate identities, failed database claims, crash recovery and all-or-nothing session reconciliation. Durable replay holds also block direct store claims/retries after managed mode is disabled. Runtime tests cover failed startup/cleanup, Windows 64-bit process handles, malformed ownership/activity responses, private Windows ACL repair and source/candidate integrity. Bridge tests exercise asynchronous login/cancel/delete and stale callbacks without real OAuth.
- `python tests/verify_opencode_accounts.py`: passed against the installed **OpenCode 2.0.27** using temporary profiles and a localhost model substitute. Two concurrent workers shared one native process, a second account used a separate process/database, and anonymous/wrong-password API calls returned 401. The actual native subscription gate rejected a profile without OAuth. The real scheduler received an explicit subscription-quota error with `Retry-After: 3600`, stopped that attempt, switched to the other account and committed one valid DOCX; this local failover completed in **4.8 seconds**. The test also proved native cancellation, unchanged source TXT, safe credential metadata projection and process shutdown. It used 14 local model requests, including title requests; none reached a paid model.
- `python tests/verify_accounts_ui.py` and `python tests/verify_ui.py .verify`: passed native Qt interactions for account opt-in, three accounts, browser/cancel/login flows through substitutes, metadata refresh, ordering/preference, stable lists and all six existing pages. Arabic screenshots were inspected at 1060×720, dark/light themes and up to 140% scaling. No QML errors were reported. The LAN browser acceptance also passed the updated worker summaries without JavaScript errors or horizontal overflow.
- Both native account acceptance scripts are included in the Windows release test gate. Account authentication uses native OAuth and the one installed OpenCode binary; Waraq settings and task reports contain no OAuth credential copies. Account storage is isolated beneath the selected application data directory.
- `python build_windows.py` completed successfully. The final portable executable passed environment discovery, `--smoke` and a Workers-page screenshot with three saved account entries and managed mode enabled, using separate verification data directories. It resolved OpenCode 2.0.27; bundled QML and mobile assets match the final source bytes. No QML errors appeared. After removing unsupported five-hour/weekly assumptions from the quota messages, the affected service/scheduler tests passed again (**213 passed**).
- **Verification boundary:** no actual ChatGPT account was signed in and no real subscription quota was consumed. The local model test substitutes the subscription gate for document requests and uses a made-up credential for serialization tests; it cannot validate a user's entitlement, live reset information or the visual quality of an actual formatted lesson. Real browser authorization and one short task with the user's chosen model/skill remain the first-use acceptance check.

## Main-window Internet shortcut — 2026-10-16

Added a global header button for Internet start/stop with adjacent connection status. An unregistered desktop opens Settings, a registered offline desktop starts the saved connector, and an enabled connecting/reconnecting connector can be stopped. Native acceptance exercises each path on the operations page, preserves the registered identity, and checks the header bounds/screenshots at 1060×720 in light/dark at 100%/140%. No automatic startup or pairing change is introduced.

## QR navigation regression — 2026-10-16

The deployed gateway returned 200 for direct `/pair` GET but 403 for the same page with `Sec-Fetch-Site: cross-site`, `Sec-Fetch-Mode: navigate`, `Sec-Fetch-Dest: document`, reproducing the Android QR report. The middleware now allows that GET navigation only for public HTML-shell routes; Host/Origin validation, cross-site API/assets/WebSocket rejection, mutation headers, pairing approval and device grants remain enforced. Added five gateway cases covering public routes, iframe/script/object/fetch rejection and protected requests. The real TLS/Edge acceptance now follows a link from an actual different site on both pairings, checks the browser's cross-site metadata and completes approval and A/B isolation.

## Repository delivery paths — 2026-10-15

GitHub's [container gate on commit f6e3def](https://github.com/M-Jadallah/WaraqStudio/actions/runs/38799704673) passed on Ubuntu, including the real production image and PostgreSQL acceptance. A Windows TLS test initially passed in CI but failed in the parallel release run; the test now binds/connects explicit IPv4 with a matching certificate SAN and allows the connector's full handshake deadline. Certificate and hostname validation remain enabled. The real local two-desktop/browser TLS stack passed again after that test-only adjustment.

The repository now has a root Arabic README covering Windows releases and Coolify deployment. Release assets include exact-commit source, gateway ZIP/checksum, provenance and SHA256SUMS; publication verifies existing tags instead of deleting them. The reusable Ubuntu relay workflow invokes `tests/verify_relay_deployment.py`, which builds the production Compose stack, probes real PostgreSQL schema/unique index, enrollment, device WebSockets, A/B denial, single-phone enforcement and persistent grants after gateway recreation, then removes only its randomly named test project's resources. Docker is unavailable locally; this container gate is configured but has not been run on this workstation. Its HTTP/WebSocket probe was separately exercised against a real local Uvicorn server with SQLite (dialect and port injected only in an ignored development harness), validating the probe protocol without claiming PostgreSQL acceptance. YAML, Python compilation and archive contents were checked locally; the full suite passed **283 tests**. Coolify v4.4.6 support for generated 64-hex secrets was verified in its tagged source; platform/domain/auto-deploy configuration remains a first-time user step.

## تحديث 2026-10-14 — دعم OpenCode 2 (نتائج سابقة)

- اكتشاف ملف npm التنفيذي من `@opencode/cli` و`opencode-ai` دون تشغيل CMD/PS1/BAT، ودعم PATH القديم في واجهة Windows والمسار اليدوي والاقتباس والمسافات وحزم ARM64 وx64. تُحترم الحزمة التي يشير إليها ملف التشغيل عند وجود الحزمتين، ولا ينتقل المسار اليدوي المعطوب إلى تثبيت آخر ضمنيًا.
- فحص إصدار محدود الزمن ومخزّن حسب مسار الملف وحجمه وتاريخ تعديله، مع مشاركة الفحص بين العمال وإعادة الفحص بعد التحديث. يعرض فحص البيئة المسار والإصدار. يحتفظ الإصدار الأول بخياراته، ويستخدم الإصدار الثاني `--standalone` و`--auto` و`provider/model#variant` ومجلد المهمة في cwd وPWD، مع فصل نص البرومبت عن الخيارات بـ`--`.
- حافظت اختبارات التنفيذ على نسخة TXT الأصلية واعتماد DOCX بعد فحصه، وعلى عزل مفاتيح العمال ومجلدات البيانات، وأضيف دعم خطأ HTTP 429 المسطّح في V2. اجتازت المجموعة `python -m pytest -q`: **203 اختبارات**.
- اجتاز الملف التنفيذي الحقيقي **OpenCode 2.0.27** فحص `python tests/verify_opencode_v2.py` بمزود HTTP محلي دون نماذج مدفوعة: قراءة قائمة النماذج، ثم إنشاء مستندين عربيين عبر أداة shell الفعلية بالتوازي بمفتاحين مختلفين، ومستوى التفكير `#high`، ومجلدي بيانات مستقلين ومسارات عربية بمسافات. ثبت أيضًا أن منع shell صراحةً يظل نافذًا مع `--auto` ولا تُعتمد مهمة بلا DOCX. لا يكتب المزود الوهمي ملفات Word بنفسه. نجح فحص البيئة من نقطة دخول التطبيق، وأعادت قائمة النماذج **29 نموذجًا** من تثبيت الجهاز الفعلي. جودة تنسيق الدروس والاتصال بحساب الإنتاج تحتاج اختبارًا بمزود المستخدم ومهارته.
- نجح `python build_windows.py`، ثم اجتازت حزمة Windows فحص البيئة (`--check-environment`) والتشغيل (`--smoke`) بمجلدَي بيانات مؤقتين مستقلين. اكتشفت الحزمة OpenCode `2.0.27` تلقائيًا. أُنشئ أرشيف محلي للنقل يضم مجلد التطبيق وملف README_AR.md دون إعدادات المستخدم أو قاعدة مهامه أو بيانات دخوله.

## تحديث 2026-10-04 — خيارات الناتج قبل استيراد التفريغات

- نُقلت قائمة قالب البرومبت ومجلد المادة إلى أعلى بطاقة إضافة المصدر، قبل أزرار استيراد TXT والمجلد. كان الاستيراد يحفظ القيم، لكن خياراتها موجودة أسفل قائمة YouTube ولا تظهر للمستخدم قبل الإضافة.
- أضيف اختيار مسار النتائج الافتراضي وحفظه فورًا للمهام الجديدة، مع إبقاء القالب وتصنيف المادة المختارين بعد إعادة نشر نماذج الإعدادات. إلغاء منتقي المسار لا يغيّر الإعدادات. القيم مشتركة مع YouTube والسحب؛ تعرض مساحة الإفلات القالب والمادة والمسار الحاليين.
- ثلاث حالات خلفية جديدة تثبت حفظ المسار والقالب غير الافتراضي والمادة للملفات والمجلد والسحب، ونسخ التفريغ تحت المسار المختار، وثبات لقطة المهمة عند تعديل القالب والمسار لاحقًا. اجتازت المجموعة المحلية 170 اختبارًا.
- اجتاز فحص Qt الأصلي اختيار المسار بالزر مع قالب مختار مسبقًا، وإلغاء اختيار المسار، وإضافة ملف وملفات ومجلد وإفلات ملفات من صفحة أخرى. تحقّق من القالب ومسار كل مهمة ونسختها المنسوخة؛ ظهرت قوائم الاختيار والمسار وأزرار TXT كاملة قبل التمرير عند 1060×720 وتكبير 140٪. فُحصت اللقطات بصريًا دون أخطاء QML. لم تُستدعَ خدمات إنتاج مدفوعة.

## تحديث 2026-10-04 — توقفات مركز العمليات والطوابير الكبيرة

- البلاغ: الإصدار `1.0.0.21`، نحو 100 عنصر، أثناء التمرير دون بحث أو فلتر، والطابور متوقف أو يعمل. كشف القياس أن عمليات `filter/map/forEach` في QML على `backend.jobs` تعيد جلب وتحويل قائمة Python كاملة عند قراءة عناصرها؛ كما كانت تغييرات سجلات العمال تعيد حساب تحديد الطابور دون تغيّر مهامه.
- أصبحت الحسابات تستخدم مصفوفة JavaScript من لقطة JSON مخزنة، وتُحدّث اللقطة وإشارة `jobsChanged` فقط عند تغيّر المهام. بقيت قائمة العرض على `DictListModel` بتحديث صفوفها في موضعها للحفاظ على التمرير والتحديد.
- في قياس 100 عنصر بنصوص أخطاء طويلة، استغرق تحديث الإشارة العامة قبل الإصلاح نحو 641 مللي ثانية وقرأ خاصية القائمة 516 مرة؛ انخفض بعده إلى نحو مللي ثانية واحدة دون قراءة `backend.jobs`. قياس 1000 عنصر خفّض تحميل QML من نحو 74 ثانية إلى نحو 1.1 ثانية. هذه قياسات محلية تشخيصية، وليست ضمانًا لتوقيت كل جهاز.
- اجتازت المجموعة 167 اختبارًا، منها أربعة اختبارات لاتساق اللقطة وإشاراتها، حفظ العربية، إعادة استخدام النسخة المخزنة، وعدم إبطالها بتغيّر مدة العامل أو السجل، وتحديثها مع تقدم المهمة وحذفها. اجتاز فحص Qt الأصلي صفحات التطبيق الست والتحديد والحذف والاستيراد والتحديث.
- أضيف `tests/verify_queue_scroll.py` إلى بوابتي CI والإصدار: أحداث عجلة Qt فعلية وسحب شريط التمرير، مع تحديث نتائج مهام أثناء التمرير وتحديد الكل، عند 1060×720 وبالواجهة المضغوطة. نجح الفحص المستقل مع 100 و500 و2000 عنصر؛ بلغ أقصى فاصل نبض للواجهة محليًا 108 و235 و959 مللي ثانية على الترتيب، ولم تظهر أخطاء QML. لكل حالة عملية مستقلة ومهلة 45 ثانية تكشف التعليق.
- حدود التشخيص: لم يتكرر تجمّد دائم في الاختبارات المحلية، لكن توقفات الواجهة وكلفة القراءة المتكررة ثبتت وأُصلحت. الفحص يستخدم طابورًا متوقفًا مع تغييرات حالات محلية؛ لا يشغّل خدمات خارجية ولا يلمس بيانات المستخدم. لا يشمل تحسين عرض آلاف الصفوف المخفية ببحث أو فلتر.
- كشف CI أيضًا نقرة اختبار مبكرة على آخر صف بعد البحث: وقع مربع التحديد خارج مساحة القائمة المقصوصة عند عدم استقرار ارتفاعات الصفوف. تكرر الفشل محليًا، ثم استُبدل تقدير `contentHeight` والانتظار الثابت بتهيئة التخطيط والتموضع بحسب فهرس الصف، مع انتظار مشروط بوجود مركز مربع التحديد داخل مساحة العرض. بقيت النقرة حدث Qt فعليًا ونتيجة التحديد شرط نجاح.
- كشف تشغيل CI لاحق أن مدد البدائل القصيرة في اختبار المسار المتوازي لا تضمن تداخل ثلاثة عمال OpenCode على جهاز أبطأ. أصبح البديل يحتفظ بأول عمال المرحلة حتى يبلغ العدد المطلوب بإشارة تزامن ذات مهلة؛ بقيت شروط عامل تنزيل واحد، عاملي Deepgram بمفتاحين مستقلين، وثلاثة عمال OpenCode على الأقل، واكتمال ملفات Word المعتمدة.

## تحديث 2026-10-04 — حذف الطابور والتحديث واعتمادية التشغيل

- حذف متعدد بمعاملة SQLite واحدة: تحديد المعرّفات وإزالة التكرارات، تصنيف العناصر النشطة والمفقودة، بقاء ترتيب العناصر الأخرى، وعدم المساس بالمصادر أو التفريغات المنسوخة أو المخرجات أو المكتبة. تثبت الاختبارات استمرار الحذف بعد فتح القاعدة من جديد، والتراجع الكامل عند فشل قاعدة البيانات، واستمرار الدفعة الصارمة بعد حذف سلف فاشل.
- اختبارات سباق حتمية للحذف مقابل claim وتسجيل العامل، والإلغاء مقابل بدء المرحلة، والإعادة/الحذف بعد completed أثناء نشر المكتبة، وtick متأخر بعد pause/shutdown. فشل إنشاء/بدء الخيط أو تجهيز العامل يحرر خانته ويعرض خطأ؛ لا يبقى عنصر نشط دون عامل.
- قارئ العملية الخارجية ينتظر EOF قبل اعتبارها منتهية، حتى لا تضيع آخر رسالة حد استخدام أو منع صلاحيات. تشمل الاختبارات فشل القارئ، خرجًا أخيرًا بلا newline، الإلغاء المبكر دون إنشاء عملية، وإلغاء/مهلة stdout الموروث دون تعليق إغلاقه.
- التحديث: مقارنة رقمية للإصدارات، رفض الرجوع لنسخة أقدم، أخطاء الوصول للمستودع الخاص، الحجم والبصمة والرابط المعتمد، ملف تنفيذي صالح، حفظ ذري، تنظيف التنزيل الملغى، بقاء المُثبّت السابق عند الفشل، وفحص البصمة مجددًا قبل التشغيل. اختبار تشغيل المُثبّت يستخدم بديلًا لـ Windows launch، ويثبت خروج التطبيق بعد نجاحه وبقاءه مفتوحًا إذا أُلغي طلب الصلاحيات.
- رمز GitHub اختياري ويُحفظ في مخزن بيانات الاعتماد؛ يثبت الاختبار غيابه عن الإعدادات وإزالته. يعمل فحص/تنزيل/تقدم التحديث عبر إشارات Qt على خيط الواجهة. يُمنع تشغيل المُثبّت أثناء الطابور أو العمال أو عمليات المكتبة أو تحديث YouTube.
- نُزّل مُثبّت الإصدار الحقيقي `v1.0.0.19` من المستودع الخاص باستخدام بيانات اعتماد التطوير في الذاكرة فقط، بحجم 150000437 بايت؛ تطابقت بصمة GitHub SHA256، وحُفظ خارج المستودع ولم يُشغَّل.
- فحص بيئة المصدر أثبت جاهزية FFmpeg وyt-dlp وEJS وNode.js وOpenCode وWindows Credential Manager. `pip check` لا يجد تعارضات. حزمة الإصدار تُحمل رقم إصدار GitHub الفعلي، وتتحقق بوابة البناء منه من داخل EXE.
- أثبت بناء GitHub رقم الحزمة `1.0.0.20` وجاهزية FFmpeg، ثم كشف تحقق التقرير عن قراءته بترميز Windows الافتراضي عند وجود رسالة عربية. أصبح أمر التحقق يقرأ التقرير صراحةً بترميز UTF-8؛ أُعيد تشغيل بوابة الإصدار كاملةً على التصحيح.
- اجتازت المجموعة المحلية 163 اختبارًا، وخمس عمليات Python مستقلة أعادت تسعة اختبارات تزامن حساسة في كل مرة (45 نجاحًا إضافيًا).
- اجتاز فحص Qt الأصلي صفحات التطبيق الست، والتحديد الفردي والمتعدد مع الفلاتر، وحذف غير النشط وبقاء النشط والملفات، وعرض Word وحده لمهام TXT مع بقاء المراحل الثلاث للعادي، وتقدم/إلغاء/أخطاء التحديث وحفظ/إزالة الرمز. تراجع بصري عند 1060×720 وتكبير 140٪ مع تمرير الإعدادات دون تجاوز شريط التنقل.
- حدود الفحص: لم تُرسل طلبات مدفوعة إلى Deepgram أو OpenCode؛ اختبارات إنتاج Word تستخدم بدائل مع اعتماد DOCX بنيويًا. يلزم قبول حي بإعدادات الخدمة وعينة درس مناسبة قبل التوزيع الواسع. المستودع ما زال خاصًا، لذلك لا تتاح إصداراته لمستخدم عام دون صلاحية؛ ينبغي توفير مصدر إصدارات عام للتوزيع العام.

## إصلاح GitHub Actions — 2026-10-04

- انهارت بوابة اختبارات Release للتغيير `d26c99a` على Windows بخطأ access violation ورمز خروج 139؛ كان خيط `oc2` يكتب إلى SQLite أثناء إغلاق اتصالها في تنظيف fixture. اكتمال حالة Word لا يعني انتهاء نسخ المكتبة أو تسجيل أحداثه؛ نجاح CI الآخر للتغيير نفسه يوافق كون الخلل سباق توقيت.
- أُضيفت أداة اختبار مشتركة تتتبع خيوط الجدولة والعمال الفعلية، وتوقف المنظّم ثم تنتظر انتهاء الجدولة والعمال قبل إغلاق قاعدة البيانات تحت قفلها. تطبقها اختبارات المحرك والاستيراد وفحص الواجهة؛ إدخالات العمال الوهمية في اختبارات التزامن لا تمنع التنظيف. بقي كود التطبيق وسير عمل GitHub كما هما.
- يشترط اختبار المسار الكامل الآن انتهاء العمال بالإضافة إلى اكتمال المراحل، ويفشل مساعد الانتظار إذا بقي العامل نشطًا بعد المهلة.
- اختباران حتميان يحجزان نشر المكتبة بعد إكمال Word حتى يبدأ تنظيف fixture، في التشغيل المباشر وفي خيط الجدولة الخلفي. يرفض اتصال محروس إغلاق SQLite قبل انتهاء النشر والخيط الخلفي، بدل المخاطرة بانهيار أصلي أثناء اختبار الانحدار.
- اجتازت المجموعة المحلية 108 اختبارات وفحص Qt الأصلي. شُغلت اختبارات المسار المتوازي وحالتَي السباق في عشر عمليات Python مستقلة، ونجحت الاختبارات الثلاثة في كل مرة دون انهيار.

## تحديث 2026-10-04 — تفريغات TXT الجاهزة إلى Word

- اجتازت مجموعة الاختبارات المحلية 106 اختبارات، منها 14 اختبارًا جديدًا للاستيراد: ملف أو ملفات TXT، مجلد بترتيب طبيعي دون المجلدات الفرعية، روابط الإفلات المحلية، إزالة التكرارات، UTF-8 وBOM، الملفات الفارغة وغير المقروءة، وحفظ قالب البرومبت ومجلد المادة.
- تحققت الاختبارات من نسخ بايتات التفريغ الأصلية إلى مجلدات المهام قبل نشرها، وتراجع دفعة كاملة عند فشل النسخ أو قاعدة البيانات، وحفظ سبب الفشل إذا تعذر تنظيف نسخة مؤقتة.
- اكتمل مسار المنظّم إلى OpenCode وخرج DOCX معتمد بنيويًا عبر بديل للعملية الخارجية، بعد حذف TXT الأصلي، مع التحقق من عدم استدعاء YouTube أو Deepgram.
- اجتاز فحص الواجهة الأصلي اختيار ملف واحد وملفات متعددة ومجلد بواسطة أزرار QML، وإفلات ملفات متعددة بأحداث Qt فعلية من صفحة العمال، ورفض إفلات الصوت. تحقّق من ظهور مساحة الإفلات، واختفائها بعدها، وحفظ القالب والمجلد وتجاوز مرحلتي الصوت.
- فُحصت الواجهة بصريًا عند 1060×720، وتُتاح خيارات المجلد والقالب والمراحل بالتمرير عند تكبير 140٪ مع إبقاء مساحة قائمة المصادر. اجتازت صفحات التطبيق الست الفحص دون أخطاء QML.
- تشمل بوابتا CI والإصدار الآن كل ملفات اختبارات Python، بما فيها اختبارات الاستيراد الجديدة. لم يُستدعَ مزوّد OpenCode حي في هذه الجولة؛ اختبار المسار استخدم عملية بديلة دون طلبات مدفوعة.

## تحديث 2026-09-23 — المجلدات وتحديث yt-dlp

- مرّت 86 تجربة محرك، منها إنشاء مجلد مادة، رفض الأسماء الخطرة والمكررة، ربط مهمة بمجلد، نقل نسخة مكتبة قديمة بين المجلدات، تصدير Word مرتين دون مضاعفة، وترحيل مهمة موجودة إلى «بدون مجلد» دون فقدها.
- مرّ فحص واجهة QML الأصلية لصفحات التطبيق الست بلا أخطاء QML. عُرضت صفحة المصدر والإعدادات وفُحص موضع اختيار المجلد وزر تحديث YouTube بصريًا.
- نُزّل `yt-dlp.exe` الرسمي للإصدار 2026.08.19 مع `SHA2-256SUMS` إلى مجلد مؤقت، ونجح التحقق من البصمة وتشغيل `--version`. النسخة المثبتة في بيئة التطوير هي 2026.08.19 مع `yt-dlp-ejs` متوافق وNode.js 24.
- فُحص الأمر المثبت لـ OpenCode 1.18.30، وتوجد فيه معاملات `run` و`--model` و`--variant` و`--dir` و`--format` المستخدمة في التطبيق. طابقت إعدادات Deepgram الجديدة توثيق Nova-3 Arabic ومعامل `diarize_model`.
- لم تُرسل طلبات فعلية إلى YouTube أو Deepgram أو OpenCode في اختبار القبول هذا. يلزم اختبار فيديو قصير وحسابات الخدمات الفعلية قبل الدفعات الكبيرة.
- أُعيد بناء `Windows/WaraqStudio/WaraqStudio.exe`، ثم نجح فحص البيئة من الملف التنفيذي (yt-dlp وEJS وFFmpeg وNode.js وOpenCode وWindows Credential Manager) ونجح التقاط صفحة الإعدادات. عولج تعارض DLL في حزمة PyInstaller: نسخ Visual C++ Runtime المتوافق مع PySide6 وإزالة `icuuc.dll` المتعارض من جذر الحزمة؛ وثّقت الخطوة في `build_windows.py` لتتكرر في كل بناء.
- كُشفت نسخة `bgutil-ytdlp-pot-provider` القديمة 1.3.2 عند تدقيق PyPI، فارتفع الاعتماد إلى 2.0.0 وأُعيد بناء الحزمة به؛ `pip check` لا يظهر تعارضات في متطلبات البيئة.


## Local automated tests

73 engine/service tests passed (timing-flake checks included), covering:

- Atomic task claims under competing workers.
- Crash recovery and uncertain paid requests.
- Original lesson numbers versus custom queue order.
- Stage dependencies and strict-order barriers.
- Independent API-key routing, five Deepgram and five OpenCode workers, one downloader.
- Shared-project concurrency cap, and five distinct project labels running in parallel.
- Raw Deepgram response recovery without another request.
- Per-chunk resume without retranscribing already saved chunks.
- Real FFmpeg WAV-to-MP3 conversion while retaining the local source.
- Literal subprocess arguments and cancellation of a real child process.
- Invalid/empty Word rejection and reuse only after committed validation.
- Revision creation while retaining prior files.
- Configuration validation and YouTube 429 cooldown.
- Bounded events log via startup and hourly pruning.
- DOCX packages size-capped before any decompression work.
- OpenCode runs on a transcript copy inside its revision workspace.
- OpenCode permission auto-rejections surface as a dedicated failure message.
- Transient download failures defer with a future `next_retry` and complete on retry; the budget caps attempts and success resets the counter.
- Deepgram failures are never auto-deferred (paid-stage protection); uncertain/blocked/cancelled stay terminal.
- OpenCode quota stops detected from the provider's streamed 429 event and deferred hourly up to the configured budget, then failed with a named message.
- Deferred retries persist across `recover()`; claims respect `next_retry`; `queue_drained()` is true only when nothing is pending or running.
- Prompt-template validation (required variables, unique ids, size cap) and enqueue-time template snapshotting into the job prompt.
- Default pools: five Deepgram workers with distinct projects and five OpenCode workers that all follow the user's own settings — no automatically created router workers.
- Pool migration: missing workers are cloned from the first saved one (keeping its model, reasoning level and provider), saved workers are never relabelled, and a worker the user deletes is never resurrected.
- Cleanup migration: the five OpenRouter workers an earlier release created are removed, the pool stays at five workers on the user's own settings, and a hand-made worker that merely uses a router provider is left alone.
- Provider validation: unknown provider on a worker, malformed provider id, id colliding with a built-in, malformed key variable, and non-boolean isolation are all rejected.
- Model-id composition per provider and the worker environment: isolated data/state directories plus the provider key for key-based workers, an untouched environment for login workers, and a user-defined provider fed the environment variable it declares.
- OpenCode command building: composed provider/model id, `--variant` dropped when empty, provider key present only for key-based workers.
- Key-based worker without a stored credential is parked with an explicit message while login workers keep running.
- A queue-level override rewrites provider, model and reasoning level for every OpenCode worker, reads the credential scoped to the forced provider, falls back to the worker's model when no forced model is given, restores each worker on clearing, and refuses an unknown provider.
- Completed stages mirror into the library; `library_auto` off leaves the mirror untouched.
- Library publish is idempotent, collision-safe for equal titles, refreshes a regenerated artifact, copies audio only when enabled, and deletion refuses paths outside the library folders.

YouTube retry rework (this round):

- A parametrised classification matrix maps yt-dlp/network failures to the policy kinds: 429 → `rate_limited`, network timeouts/DNS/resets and HTTP 5xx → `failed`, sign-in/cookies/403 → `blocked`, and deleted/private/geo/copyright/404/unsupported URLs → `permanent` (never retried).
- A 429 sets a persisted cooldown (honouring `Retry-After` when present), the cooldown survives constructing a fresh `Youtube` on the same store, and expiry is cleared lazily.
- A rate-limited download is deferred past the shared cooldown without any service pause or automatic block, then completes on the scheduled retry; other downloads stay gated until the cooldown expires and resume automatically afterwards.
- Exhausting `youtube_retry_attempts` ends in a named terminal failure without stopping the service; a permanent video failure fails once, leaves the stage guards untouched, and the next download in the queue runs normally.
- A cookies/403 block sets an automatic download-stage block (manual pause set stays empty); manual retry plus `unblock_stage()` requeues the job and it completes.
- The user's manual service pause survives a download retry and keeps the job parked until resumed.
- Low free disk space blocks the stage automatically with a visible reason; after space is freed the retry/re-check path requeues the job and the download proceeds.
- `youtube_retry_attempts` is seeded once from `auto_retry_attempts` for existing settings files and defaults to 3 for fresh installs; validation bounds (0–6) apply.

Upgrade rehearsal on copies of the real settings and task database: the `runtime_state` table is created non-destructively, all 22 jobs and 66 stages are preserved, the five OpenCode workers and seven Deepgram workers stay as saved, and the smoke run exits 0.

UI density round (this round):

- All six data lists (operations, source, workers, library, files, providers) bind to `DictListModel` bridge models, so backend updates land as in-place row changes and never reset the scroll; a native check scrolls the jobs list, applies a real update (a new queue row plus a new log line) while scrolled and asserts both the offset and the row count survive.
- List density is switchable: `ui_compact` (on by default) shrinks rows, paddings and gaps together through the `rh()`/`rs()`/`density` helpers — jobs 70→50, source 54→39, workers 96→69, library 72→52, files 62→45, providers 56→40 — while fonts stay on `fs()`, and the Settings checkbox applies and persists it.
- Every page was compressed: shorter rows (jobs 89→70, source 64→54, workers 126→96, library 100→72, files 78→62), smaller buttons/fields, tighter margins and a single compact header — screenshots reviewed at 1060×720 for all six pages plus dialogs.
- The navigation rail collapses 220↔64 px with icon-only mode and tooltips; the state persists through `ui_rail_collapsed` and navigation keeps working while collapsed (verified by clicking through to the library page).
- `ui_scale` (0.8–1.4) scales fonts and control metrics live through the shared `fs()` helper and the button/field primitives; the Settings slider applies and persists it, and a saved 1.2 value updates the window property and re-scales the interface.
- Validation rejects `ui_scale` outside 0.8–1.4 (including strings and booleans) and non-boolean `ui_rail_collapsed`; the template carries both defaults.

Native UI interaction checks passed for all six screens, range selection, custom ordering, queue insertion, template publication/save/enqueue snapshotting, the auto-stop toggle wiring, direct worker enable toggles, provider management (adding one, rejecting a bad id and a reserved id, refusing to delete one in use), the pre-start override question starting the queue on both answers, library organise/list/delete, service pause/resume, the automatic-download-block flow (block reason visible on the YouTube worker row, re-check lifts it and requeues the parked job), scroll preservation across live model updates, the collapsible navigation rail, the live UI-scale setting, theme persistence and a 1060 × 720 window. No QML errors were reported. Screenshots were inspected at 1060 × 720 for Arabic shaping, reading order, contrast, spacing, modal readability and overflow. The interface is a native Qt application, so verification used Qt Test and QQuickWindow screenshots rather than a web browser.

The source environment resolves FFmpeg, yt-dlp, EJS, Requests, OpenPyXL, Windows Credential Manager, Node.js and the installed OpenCode executable.

## LAN phone control — 2026-10-15

- `python -m pytest tests -q --tb=short`: **231 passed**, including 28 LAN tests. Coverage includes authentication and expiry, Arabic numerals, HttpOnly/SameSite cookies, session revocation/logout, per-peer throttling, Host/DNS-rebinding and cross-origin rejection, public-peer rejection, JSON/custom-header mutation checks, allowlisted snapshots, GUI-thread dispatch, explicit queue state/idempotence, cancellation, uncertain paid retry confirmation, separate source discovery, stale/double enqueue prevention, selected template/folder snapshots, byte-preserving uploaded transcripts, atomic invalid-upload rejection, Windows reserved filenames, size limits, confined authenticated downloads, a real Waitress HTTP request, socket shutdown, failed-bind cleanup/restart, timeout cancellation and revocation before dispatch.
- `python tests/verify_ui.py .verify`: native controls passed for starting LAN access on a selected port, SVG QR loading, local pairing, code renewal revoking the old session, and stopping the listener. Existing six-page interaction checks also passed with no QML errors. Local offscreen runs set `QT_QPA_FONTDIR=C:\Windows\Fonts` to use the installed Windows fonts.
- `python tests/verify_mobile.py .verify`: real headless Edge exercised the served page and real HTTP server with a temporary database and service doubles. Passed pairing/wrong-code feedback, queue start/pause, retry, YouTube selection/enqueue, UTF-8/BOM upload, service pause/resume, authenticated Word/TXT downloads, malicious-title text rendering, persistent cookies on reload, logout, offline controls and recovery. Both themes passed at 360, 375, 768, 844 (landscape), and 1440 pixels, plus enlarged text; no horizontal overflow or JavaScript errors. Screenshots were inspected.
- Windows bundle built successfully. Its `--check-environment` exited 0 and reported all mobile assets, Flask, Waitress and SVG QR generation present, along with the existing FFmpeg/yt-dlp/OpenCode checks. The bundled settings-page screenshot run also exited 0 without QML errors. All three bundled mobile assets match the source bytes. Browser acceptance additionally passed using the host's actual LAN IPv4 URL from the same computer; this does not test inbound access from a separate physical phone.
- The LAN server uses [Waitress, documented by Flask as supporting Windows](https://flask.palletsprojects.com/en/stable/deploying/waitress/). GUI requests follow [Qt's queued signal/slot thread rules](https://doc.qt.io/qtforpython-6/tutorials/basictutorial/signals_and_slots.html); viewport/theme checks use [Playwright's browser emulation](https://playwright.dev/python/docs/emulation).
- Physical-phone access through the router/firewall remains a user-side check. The current host's Wi-Fi profile was observed as **Public**; a trusted home network must be configured appropriately and Waraq allowed through Windows Firewall on private networks. No network-category or firewall settings were changed. LAN HTTP is intended for a trusted network, with no public port forwarding.

## Internet phone control — 2026-10-15

Follow-up for the supplied domain `waraqstudio.jadallah.work`, Coolify v4.4.6 and one phone per device: **283 tests passed**, including **30 gateway tests**. Added separate-browser refusal, explicit revoke/new-QR replacement, approved-slot reservation, stale/parallel claims and polling, expired/idle session recovery and legacy-index upgrade checks without silent data deletion. Internet/LAN browser acceptance and the real TLS two-desktop stack passed again. Native acceptance checks the supplied default domain, one-phone policy and actionable gateway rejection text; 100/140% light/dark layout checks passed. The Windows bundle was rebuilt with these updates. Public Docker/PostgreSQL/Coolify deployment remains a user-run acceptance step, described in `deploy/relay/COOLIFY_SETUP_AR.md`.

- Full unit/service suite: **275 passed**, including 22 connector tests, 22 gateway tests and the existing 28 LAN tests. Coverage includes one-use enrollment, credential isolation, authenticated device sockets, conflicting identities, approval, persistent scoped grants, A/B state/action/download denial, revocation, Host/Origin/CSRF, upload admission and envelope limits, large snapshots, streamed download limits/failures, cancellation, deadlines, persistent mutation deduplication and uncertain crash recovery. Gateway database tests use SQLite through the production SQLAlchemy models.
- `python tests/verify_internet_stack.py .verify/internet-stack`: real local TLS Uvicorn gateway, two actual Qt Bridges/connectors and headless Edge passed HTTPS enrollment, WSS, phone approval, malicious A/B routing, queue isolation, byte-preserving BOM TXT upload/download, cancellation, persistent browser sessions, explicit second pairing/device picker, A offline with B still available, scoped revocation and clean shutdown. Paid scheduler work was replaced; test certificates and an in-memory credential store were used. TLS hostname verification stayed enabled in Python; Edge accepted only the temporary test environment's certificate.
- `python tests/verify_mobile_internet.py .verify/internet-mobile`: phone UI acceptance passed pairing fragment clearing, approval, device switching, delayed replies, upload/download scoping, offline/recovery, uncertain operation feedback, session persistence and logout. Light/dark responsive screenshots passed with no JavaScript errors or horizontal overflow. This browser-only suite substitutes HTTP responses; authorization is covered by the integrated and gateway tests above.
- `python tests/verify_internet_ui.py .verify/internet-native`: native registration, secret-field clearing, QR, approvals/rejections, revocation, forget confirmation, and independent Internet/LAN/queue lifecycle passed at 1060×720, 100%/140%, both themes. Management replies were substituted in this native UI suite. The existing six-page `verify_ui.py` also passed without QML errors.
- New Windows portable bundle built successfully. `--check-environment` exited 0 with `internet_connector: true`, bundled mobile assets, QR, Flask and Waitress present. The bundled settings screenshot exited 0 with no QML errors; all mobile asset bytes match the source.
- Deployment Compose was parsed and checked for absence of public service/database port mappings. **No Docker engine is installed on this workstation: the image build, a live PostgreSQL instance, Coolify deployment, public DNS/TLS and a physical phone over mobile data have not yet been tested.** These remain deployment acceptance checks on the user's VPS. No server or DNS configuration was changed.

## Limits of this verification

Service workers used deterministic test substitutes where network/paid providers would otherwise be called. No real YouTube download, Deepgram transcription or OpenCode document generation was performed for acceptance. DOCX tests check structure and editable text, not the visual fidelity of an actual lesson produced by the configured model.

Before a large batch, test one short video and inspect the Word result. YouTube restrictions and provider quotas remain external constraints. API keys and authentication data are not included in the release folder.
