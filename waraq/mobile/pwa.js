/* Internet-only PWA enhancement; never persists pairing links or secrets. */
(() => {
  'use strict';
  if (location.protocol !== 'https:' || !isSecureContext ||
      !(/^\/(internet|pair)(\/)?$/.test(location.pathname) || /^\/devices\/[A-Za-z0-9_-]+$/.test(location.pathname))) return;
  const standalone = matchMedia('(display-mode: standalone)').matches || navigator.standalone === true;
  const ios = /iPhone|iPad|iPod/.test(navigator.userAgent) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
  const busy = () => {
    try { return !window.waraqPwaBridge || window.waraqPwaBridge.isBusy() || dialog.open; }
    catch (_) { return true; }
  };
  for (const [rel, href] of [['manifest','/manifest.webmanifest'],['apple-touch-icon','/assets/icons/apple-touch-icon.png']]) {
    const link = document.createElement('link'); link.rel = rel; link.href = href; document.head.append(link);
  }
  const capable = document.createElement('meta'); capable.name = 'apple-mobile-web-app-capable'; capable.content = 'yes'; document.head.append(capable);
  const controls = document.createElement('aside'); controls.id = 'pwa-controls'; controls.className = 'panel pwa-controls';
  controls.setAttribute('aria-label', 'تثبيت ورق والاقتران');
  controls.innerHTML = '<button id="pwa-install" class="quiet" hidden>تثبيت ورق</button> <button id="pwa-install-help" class="quiet">استخدام ورق كتطبيق</button> <button id="pwa-pair" class="quiet">ربط كمبيوتر</button> <button id="pwa-update" class="quiet" hidden>تحديث الواجهة</button><p id="pwa-message" class="muted" role="status"></p>';
  document.querySelector('main')?.prepend(controls);
  const $ = id => document.getElementById(id);
  const message = text => { $('pwa-message').textContent = text; };
  const dialog = document.createElement('dialog'); dialog.id = 'pwa-pair-dialog'; dialog.className = 'panel';
  dialog.setAttribute('aria-labelledby','pwa-pair-title');
  dialog.innerHTML = '<h2 id="pwa-pair-title">ربط الكمبيوتر داخل ورق</h2><p class="muted">اعرض QR جديدًا من ورق على الكمبيوتر، ثم اقرأه هنا. يقبل الكمبيوتر هاتفًا واحدًا؛ ألغِ الاقتران القديم منه عند الحاجة.</p><video id="pwa-camera" playsinline muted hidden aria-label="قراءة رمز الاقتران"></video><p id="pwa-camera-error" class="error" role="alert"></p><button id="pwa-scan" class="quiet">فتح كاميرا QR</button><form id="pwa-link-form"><label for="pwa-link">أو الصق رابط الاقتران الكامل</label><input id="pwa-link" type="text" autocomplete="off" spellcheck="false" dir="ltr" maxlength="2048" required><button type="submit" class="primary">متابعة الاقتران</button></form><button id="pwa-pair-close" class="quiet">إغلاق</button>';
  document.body.append(dialog);
  let installEvent = null, registration = null, stream = null, scanEpoch = 0, decoderPromise = null;
  let cameraOpening = false, updateArmed = false, updateLock = false, unlockTimer = null;
  const video = $('pwa-camera');
  function stopCamera() {
    ++scanEpoch; cameraOpening = false;
    if (stream) for (const track of stream.getTracks()) track.stop();
    stream = null; video.pause(); video.srcObject = null; video.hidden = true;
    $('pwa-scan').disabled = false;
  }
  function closePair() { stopCamera(); $('pwa-link').value = ''; dialog.close(); }
  function validPairLink(value) {
    if (typeof value !== 'string' || value.length > 2048) throw Error('ألصق رابط الاقتران الكامل من ورق على هذا الدومين.');
    const url = new URL(value.trim());
    if (url.origin !== location.origin || url.pathname !== '/pair' || url.search || url.username || url.password ||
        !/^#[A-Za-z0-9_-]{20,200}$/.test(url.hash)) throw Error('رابط الاقتران يجب أن يخص بوابة ورق الحالية فقط.');
    return url.href;
  }
  async function accept(value) {
    stopCamera(); $('pwa-link').value = '';
    try {
      const url = validPairLink(value);
      const bridge = window.waraqPwaBridge;
      if (!bridge || bridge.isBusy()) throw Error('انتظر اكتمال العملية الحالية قبل الاقتران.');
      const accepted = await bridge.acceptPairLink(url);
      if (accepted === false) throw Error('تعذر بدء الاقتران. انتظر اكتمال العملية الحالية.');
      closePair();
    } catch (error) { $('pwa-camera-error').textContent = error.message || 'رابط غير صالح.'; }
    value = null;
  }
  function decoder() {
    if (window.jsQR) return Promise.resolve(window.jsQR);
    if (!decoderPromise) decoderPromise = new Promise((resolve, reject) => {
      const script = document.createElement('script'); script.src = '/assets/vendor/jsQR.js';
      script.onload = () => window.jsQR ? resolve(window.jsQR) : reject(Error('decoder'));
      script.onerror = () => { decoderPromise = null; script.remove(); reject(Error('decoder')); };
      document.head.append(script);
    });
    return decoderPromise;
  }
  async function scan() {
    if (cameraOpening || stream) return;
    $('pwa-camera-error').textContent = '';
    if (!navigator.mediaDevices?.getUserMedia) { $('pwa-camera-error').textContent = 'الكاميرا غير متاحة؛ الصق الرابط بدلًا منها.'; return; }
    cameraOpening = true; $('pwa-scan').disabled = true;
    const epoch = ++scanEpoch;
    try {
      const acquired = await navigator.mediaDevices.getUserMedia({video:{facingMode:{ideal:'environment'}}, audio:false});
      if (epoch !== scanEpoch || !dialog.open) { acquired.getTracks().forEach(track => track.stop()); return; }
      stream = acquired; video.srcObject = stream; video.hidden = false; await video.play();
      let native = null;
      if ('BarcodeDetector' in window) {
        try { native = new BarcodeDetector({formats:['qr_code']}); } catch (_) { /* local fallback below */ }
      }
      const fallback = native ? null : await decoder();
      const canvas = document.createElement('canvas'), context = canvas.getContext('2d',{willReadFrequently:true});
      async function frame() {
        if (epoch !== scanEpoch || !stream || !dialog.open) return;
        try {
          let found;
          if (native) { const codes = await native.detect(video); found = codes[0]?.rawValue; }
          else if (video.readyState >= 2 && video.videoWidth) {
            const factor = Math.min(1, 640/video.videoWidth);
            canvas.width = Math.round(video.videoWidth*factor); canvas.height = Math.round(video.videoHeight*factor);
            context.drawImage(video,0,0,canvas.width,canvas.height);
            const pixels = context.getImageData(0,0,canvas.width,canvas.height);
            found = fallback(pixels.data,pixels.width,pixels.height,{inversionAttempts:'attemptBoth'})?.data;
          }
          if (epoch !== scanEpoch) return;
          if (found) { await accept(found); return; }
          setTimeout(frame,180);
        } catch (_) { stopCamera(); $('pwa-camera-error').textContent = 'تعذرت قراءة الكاميرا؛ الصق رابط الاقتران.'; }
      }
      await frame();
    } catch (_) {
      if (epoch !== scanEpoch) return;
      stopCamera(); $('pwa-camera-error').textContent = 'لم تُفتح الكاميرا. يمكنك السماح بها أو لصق رابط الاقتران.';
    }
  }
  $('pwa-pair').onclick = () => {
    if (/^\/devices\//.test(location.pathname)) { message('افتح «أجهزتي» أولًا، ثم اختر ربط كمبيوتر.'); return; }
    if (updateLock) return;
    if (busy()) { message('انتظر اكتمال العملية الحالية قبل الاقتران.'); return; }
    $('pwa-camera-error').textContent = ''; dialog.showModal(); $('pwa-link').focus();
  };
  $('pwa-scan').onclick = scan;
  $('pwa-pair-close').onclick = closePair;
  dialog.addEventListener('cancel', stopCamera);
  dialog.addEventListener('close', () => { stopCamera(); $('pwa-link').value = ''; });
  $('pwa-link-form').onsubmit = event => { event.preventDefault(); accept($('pwa-link').value); };
  addEventListener('pagehide', () => { stopCamera(); $('pwa-link').value = ''; });
  document.addEventListener('visibilitychange', () => { if (document.hidden) stopCamera(); });
  addEventListener('beforeinstallprompt', event => {
    event.preventDefault(); installEvent = event; $('pwa-install').hidden = standalone;
  });
  addEventListener('appinstalled', () => { installEvent = null; $('pwa-install').hidden = true; message('ثُبّت ورق. افتحه من أيقونته واقترن بالكمبيوتر من داخله عند الحاجة.'); });
  $('pwa-install').onclick = async () => {
    if (!installEvent) return;
    const event = installEvent; installEvent = null; $('pwa-install').hidden = true;
    await event.prompt(); await event.userChoice;
  };
  $('pwa-install-help').hidden = standalone;
  $('pwa-install-help').onclick = () => message(ios ?
    'على iPhone: افتح هذه الصفحة في Safari، ثم مشاركة ← إضافة إلى الشاشة الرئيسية. افتح ورق من الأيقونة واربط الكمبيوتر من داخله. قد يلزم إلغاء الاقتران السابق من الكمبيوتر أولًا.' :
    'من قائمة متصفحك اختر تثبيت التطبيق أو إضافة إلى الشاشة الرئيسية إن ظهر الخيار. افتح ورق من الأيقونة واربط الكمبيوتر من داخله عند الحاجة.');
  if ('serviceWorker' in navigator) {
    function unlock() {
      clearTimeout(unlockTimer); updateLock = false;
      window.waraqPwaBridge?.setUpdateLock?.(false);
    }
    navigator.serviceWorker.addEventListener('message', event => {
      if (event.data?.type === 'WARAQ_UPDATE_CHECK' && event.ports[0]) {
        const idle = !busy() && typeof window.waraqPwaBridge?.setUpdateLock === 'function';
        if (idle) {
          updateLock = true; window.waraqPwaBridge.setUpdateLock(true);
          clearTimeout(unlockTimer); unlockTimer = setTimeout(unlock,15000);
        }
        event.ports[0].postMessage({idle});
      }
      if (event.data?.type === 'WARAQ_UPDATE_UNLOCK') unlock();
      if (event.data?.type === 'WARAQ_UPDATE_BLOCKED') { updateArmed = false; message('التحديث ينتظر اكتمال العمليات وإغلاق حوار الاقتران في كل النوافذ. ثم اضغط تحديث الواجهة.'); }
    });
    navigator.serviceWorker.addEventListener('controllerchange', () => {
      unlock();
      // Never reload another tab unexpectedly; it can update on next visit.
      if (updateArmed && !busy()) location.reload();
    });
    navigator.serviceWorker.register('/sw.js',{scope:'/',updateViaCache:'none'}).then(reg => {
      registration = reg;
      const waiting = () => { if (reg.waiting) $('pwa-update').hidden = false; };
      waiting(); reg.addEventListener('updatefound', () => reg.installing?.addEventListener('statechange', waiting));
      $('pwa-update').onclick = () => {
        if (busy()) { message('انتظر اكتمال العملية الحالية قبل تحديث الواجهة.'); return; }
        if (registration.waiting) { updateArmed = true; registration.waiting.postMessage({type:'WARAQ_APPLY_UPDATE'}); }
      };
    }).catch(() => message('التثبيت دون اتصال غير متاح الآن؛ يمكنك متابعة الاستخدام في المتصفح.'));
  }
})();
