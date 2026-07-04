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

  function dataURLtoBlob(dataURL) {
    const [head, b64] = dataURL.split(',');
    const mime = (head.match(/:(.*?);/) || [, 'image/jpeg'])[1];
    const bin = atob(b64);
    const arr = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
    return new Blob([arr], { type: mime });
  }

  window.MTIBridge = { collect, dataURLtoBlob, sendImage };
})();
