/* Write-key and device-ID generators (plan §26).
 *
 * Both run entirely in the browser using crypto.getRandomValues. Nothing
 * generated here is sent anywhere: the write key reaches the server for the
 * first time when the device makes its first measurement, and only as a hash
 * thereafter. That is the whole point — the server never generates or stores a
 * key it could later hand back, so there is nothing to leak and nothing to
 * recover.
 */
(function () {
  "use strict";

  // URL-safe alphabet, minus the characters that are easy to confuse when a
  // key is copied by hand off a screen (0/O, 1/l/I).
  var KEY_ALPHABET = "abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789-_";
  var ID_ALPHABET = "abcdefghijkmnopqrstuvwxyz23456789";

  /* Uniform random string. Rejection sampling rather than `% alphabet.length`,
   * which would bias the first few characters of the alphabet — a small bias,
   * but free to avoid. */
  function randomString(length, alphabet) {
    var out = "";
    var max = 256 - (256 % alphabet.length);
    var buffer = new Uint8Array(length * 2);
    while (out.length < length) {
      crypto.getRandomValues(buffer);
      for (var i = 0; i < buffer.length && out.length < length; i++) {
        if (buffer[i] < max) {
          out += alphabet.charAt(buffer[i] % alphabet.length);
        }
      }
    }
    return out;
  }

  /* 32 characters of a 58-character alphabet is about 187 bits — comfortably
   * past the 128 bits the plan asks for, and still one line to copy. */
  function generateWriteKey() {
    return randomString(32, KEY_ALPHABET);
  }

  /* Device IDs are public, so this is about collision resistance, not secrecy.
   * 12 characters of a 33-character alphabet is roughly 60 bits. */
  function generateDeviceId() {
    return "dev-" + randomString(12, ID_ALPHABET);
  }

  function announce(button, message) {
    var original = button.dataset.label || button.textContent;
    button.dataset.label = original;
    button.textContent = message;
    button.classList.add("is-done");
    setTimeout(function () {
      button.textContent = original;
      button.classList.remove("is-done");
    }, 1500);
  }

  function copy(text, button) {
    function fallback() {
      // clipboard.writeText needs a secure context; a plain-HTTP deployment
      // would otherwise get a button that silently does nothing.
      var field = document.createElement("textarea");
      field.value = text;
      field.setAttribute("readonly", "");
      field.style.position = "fixed";
      field.style.opacity = "0";
      document.body.appendChild(field);
      field.select();
      try {
        document.execCommand("copy");
        announce(button, "Copied");
      } catch (err) {
        announce(button, "Press Ctrl+C");
      }
      document.body.removeChild(field);
    }

    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(function () {
        announce(button, "Copied");
      }, fallback);
    } else {
      fallback();
    }
  }

  function wire(root) {
    var output = root.querySelector("[data-output]");
    var generator = root.dataset.generator === "device-id" ? generateDeviceId : generateWriteKey;

    root.querySelectorAll("[data-action='generate']").forEach(function (button) {
      button.addEventListener("click", function () {
        output.value = generator();
        output.removeAttribute("placeholder");
      });
    });

    root.querySelectorAll("[data-action='copy']").forEach(function (button) {
      button.addEventListener("click", function () {
        if (!output.value) {
          output.value = generator();
        }
        copy(output.value, button);
      });
    });
  }

  document.querySelectorAll("[data-generator]").forEach(wire);
})();
