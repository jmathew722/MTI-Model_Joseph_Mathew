/* Parent-side bridge to the (unmodified) photo app in the Tab 1 iframe.
 *
 * The photo app declares its state with `let` (`let savedCrops = []`), so those
 * bindings are NOT properties of the iframe's window and cannot be read by
 * property access from here. Instead we append a <script> into the iframe's own
 * document at Run time: that script runs in the same global lexical scope as the
 * photo app, so it CAN read `savedCrops` / `img` / `stem()`. It converts the
 * queued PNG crops (and the full loaded drawing) to JPEG data URLs and posts them
 * back to us. The photo app's source files are never modified.
 */
(function () {
  // Runs INSIDE the iframe (stringified). Reads the photo app's own state.
  function injected() {
    (async () => {
      function toJpeg(imageLike, w, h) {
        const c = document.createElement('canvas');
        c.width = w; c.height = h;
        c.getContext('2d').drawImage(imageLike, 0, 0, w, h);
        // High JPEG quality (0.97) at the image's NATIVE resolution — fine dimension
        // text on drawings is easily lost to compression, so keep it crisp.
        return c.toDataURL('image/jpeg', 0.97);
      }
      function loadBlob(blob) {
        return new Promise((res, rej) => {
          const i = new Image();
          i.onload = () => res(i);
          i.onerror = rej;
          i.src = URL.createObjectURL(blob);
        });
      }
      try {
        // eslint-disable-next-line no-undef
        if (typeof savedCrops === 'undefined' || !savedCrops.length) {
          parent.postMessage({ __mti: 'crops', error: 'no-crops' }, '*');
          return;
        }
        const crops = [];
        // eslint-disable-next-line no-undef
        for (const c of savedCrops) {
          const im = await loadBlob(c.blob);
          crops.push({ name: c.name, dataURL: toJpeg(im, im.naturalWidth, im.naturalHeight) });
        }
        let source = null;
        // eslint-disable-next-line no-undef
        if (typeof img !== 'undefined' && img) {
          // eslint-disable-next-line no-undef
          source = toJpeg(img, img.naturalWidth, img.naturalHeight);
        }
        // eslint-disable-next-line no-undef
        const s = (typeof stem === 'function') ? stem() : 'drawing';
        parent.postMessage({ __mti: 'crops', stem: s, source, crops }, '*');
      } catch (e) {
        parent.postMessage({ __mti: 'crops', error: String(e && e.message || e) }, '*');
      }
    })();
  }

  const INJECTED_SRC = '(' + injected.toString() + ')();';

  // Collect the queued crops + source image from the photo app iframe.
  // Resolves { stem, source(dataURL|null), crops:[{name, dataURL}] }.
  function collect(iframe, timeoutMs) {
    timeoutMs = timeoutMs || 15000;
    return new Promise((resolve, reject) => {
      function onMsg(ev) {
        const d = ev.data;
        if (!d || d.__mti !== 'crops') return;
        cleanup();
        if (d.error) reject(new Error(d.error === 'no-crops'
          ? 'Queue at least one view in the photo app first.' : d.error));
        else resolve(d);
      }
      const timer = setTimeout(() => {
        cleanup();
        reject(new Error('Timed out reading crops from the photo app.'));
      }, timeoutMs);
      function cleanup() {
        window.removeEventListener('message', onMsg);
        clearTimeout(timer);
      }
      window.addEventListener('message', onMsg);
      try {
        const doc = iframe.contentDocument || iframe.contentWindow.document;
        const s = doc.createElement('script');
        s.textContent = INJECTED_SRC;
        doc.body.appendChild(s);
      } catch (e) {
        cleanup();
        reject(new Error('Could not reach the photo app frame: ' + e.message));
      }
    });
  }

  // Load an image INTO the photo app (Tab 1 cropper) from the parent — used so
  // converted DWG/eDrawings images open in the cropper, which cannot read the
  // proprietary formats itself. Injects a script that calls the photo app's own
  // global loadFile(file); photo app sources stay unmodified.
  function sendImage(iframe, dataURL, name) {
    return new Promise((resolve, reject) => {
      try {
        const doc = iframe.contentDocument || iframe.contentWindow.document;
        const s = doc.createElement('script');
        const payload = JSON.stringify({ dataURL, name: name || 'converted.png' });
        s.textContent = '(async () => {\n' +
          '  const p = ' + payload + ';\n' +
          '  try {\n' +
          '    const blob = await (await fetch(p.dataURL)).blob();\n' +
          '    const f = new File([blob], p.name, { type: blob.type || "image/png" });\n' +
          '    if (typeof loadFile === "function") loadFile(f);\n' +
          '    parent.postMessage({ __mti: "img-loaded", ok: true }, "*");\n' +
          '  } catch (e) {\n' +
          '    parent.postMessage({ __mti: "img-loaded", ok: false, error: String(e) }, "*");\n' +
          '  }\n' +
          '})();';
        function onMsg(ev) {
          const d = ev.data;
          if (!d || d.__mti !== 'img-loaded') return;
          window.removeEventListener('message', onMsg);
          d.ok ? resolve() : reject(new Error(d.error));
        }
        window.addEventListener('message', onMsg);
        doc.body.appendChild(s);
        setTimeout(() => { window.removeEventListener('message', onMsg); resolve(); }, 5000);
      } catch (e) {
        reject(new Error('Could not reach the photo app frame: ' + e.message));
      }
    });
  }

  // Let the photo app (Tab 1) ACCEPT CAD files it cannot decode itself, and
  // MIRROR whatever it displays to the parent. Injects a script that
  // (a) adds .dwg/.dxf/.edrw/.eprt/.easm to the file input's accept list,
  // (b) wraps the app's global loadFile so CAD picks (file dialog OR drag-drop)
  //     are posted to the parent as {__mti:'cad-open'} for server conversion,
  // (c) after any image/PDF loads into the cropper, posts the rendered drawing
  //     as {__mti:'src-open'} so Tab 2's "input document" box shows the same
  //     file that is open in the Tab 1 viewer.
  // Photo app sources stay unmodified.
  function hookCadIntake(iframe) {
    function hook() {
      if (window.__mtiCadHook) return;
      var CAD = /\.(dwg|dxf|edrw|eprt|easm)$/i;
      var fi = document.getElementById('fileInput');
      if (typeof window.loadFile !== 'function' || !fi) return; // app not ready yet
      window.__mtiCadHook = true;
      fi.setAttribute('accept', (fi.getAttribute('accept') || 'image/*') +
        ',.dwg,.dxf,.edrw,.eprt,.easm');
      // Once the cropper's global `img` shows the new file (its src moved past
      // `prev`), send a JPEG of it to the parent. `img` is a top-level `let` in
      // the photo app — visible here because this script shares its global scope.
      function postSource(name, prev) {
        var tries = 0;
        var t = setInterval(function () {
          try {
            /* eslint-disable no-undef */
            if (typeof img !== 'undefined' && img && img.naturalWidth > 0 &&
                img.src && img.src !== prev) {
              var c = document.createElement('canvas');
              c.width = img.naturalWidth; c.height = img.naturalHeight;
              c.getContext('2d').drawImage(img, 0, 0);
              parent.postMessage({ __mti: 'src-open', name: name,
                                   dataURL: c.toDataURL('image/jpeg', 0.95) }, '*');
              clearInterval(t);
            }
            /* eslint-enable no-undef */
          } catch (e) { clearInterval(t); }
          if (++tries > 60) clearInterval(t); // PDFs can take a while to rasterize
        }, 250);
      }
      var orig = window.loadFile;
      window.loadFile = function (file) {
        if (file && CAD.test(file.name || '')) {
          file.arrayBuffer().then(function (buf) {
            parent.postMessage({ __mti: 'cad-open', name: file.name, buf: buf }, '*', [buf]);
          });
          return;
        }
        // eslint-disable-next-line no-undef
        var prev = (typeof img !== 'undefined' && img) ? img.src : '';
        var r = orig.apply(this, arguments);
        if (file && file.name) postSource(file.name, prev);
        return r;
      };
    }
    function inject() {
      try {
        const doc = iframe.contentDocument || iframe.contentWindow.document;
        if (!doc || !doc.body || iframe.contentWindow.__mtiCadHook) return;
        const s = doc.createElement('script');
        s.textContent = '(' + hook.toString() + ')();';
        doc.body.appendChild(s);
      } catch (e) { /* frame not reachable yet; the load listener retries */ }
    }
    iframe.addEventListener('load', inject);
    inject(); // the iframe usually finished loading long before first use
    // Photo app defines loadFile in an end-of-body script; if we injected
    // before it ran, retry briefly until the hook takes.
    let tries = 0;
    const t = setInterval(() => {
      try {
        if (iframe.contentWindow.__mtiCadHook || ++tries > 50) clearInterval(t);
        else inject();
      } catch (e) { clearInterval(t); }
    }, 200);
  }

  function dataURLtoBlob(dataURL) {
    const [head, b64] = dataURL.split(',');
    const mime = (head.match(/:(.*?);/) || [, 'image/jpeg'])[1];
    const bin = atob(b64);
    const arr = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
    return new Blob([arr], { type: mime });
  }

  window.MTIBridge = { collect, dataURLtoBlob, sendImage, hookCadIntake };
})();
