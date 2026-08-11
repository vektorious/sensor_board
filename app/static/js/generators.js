/* Device-ID and write-key generators, and the code examples they fill in
 * (plan §26).
 *
 * Both generators run entirely in the browser using crypto.getRandomValues.
 * Nothing generated here is sent anywhere: the write key reaches the server for
 * the first time when the device makes its first measurement, and only as a
 * hash thereafter. That is the whole point — the server never generates or
 * stores a key it could later hand back, so there is nothing to leak and
 * nothing to recover.
 *
 * Generated values are substituted straight into the curl/Python/Arduino
 * examples, so the reader copies something that already works instead of
 * hand-editing two placeholders in three snippets. Substitution is via
 * textContent, never innerHTML, so a value can never become markup.
 *
 * The values are deliberately *not* persisted. Writing a write key to
 * localStorage would leave a secret on the machine long after the tab closed,
 * to save one click.
 */
(function () {
  "use strict";

  // URL-safe alphabet, minus the characters that are easy to confuse when a
  // key is copied by hand off a screen (0/O, 1/l/I).
  var KEY_ALPHABET = "abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789-_";
  var ID_ALPHABET = "abcdefghijkmnopqrstuvwxyz23456789";

  var GENERATORS = {
    "device-id": generateDeviceId,
    "write-key": generateWriteKey
  };

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

  /* Substitute one field into every example that references it. */
  function fillExamples(field, value) {
    document.querySelectorAll('[data-field="' + field + '"]').forEach(function (slot) {
      slot.textContent = value;
      slot.classList.add("is-filled");
    });
    var banner = document.getElementById("examples-filled");
    if (banner) {
      banner.hidden = false;
    }
  }

  function generateInto(panel) {
    var field = panel.dataset.generator;
    var value = GENERATORS[field]();
    panel.querySelector("[data-output]").value = value;
    panel.querySelector("[data-output]").removeAttribute("placeholder");
    fillExamples(field, value);
    return value;
  }

  function wirePanel(panel) {
    var output = panel.querySelector("[data-output]");

    panel.querySelectorAll("[data-action='generate']").forEach(function (button) {
      button.addEventListener("click", function () {
        generateInto(panel);
      });
    });

    panel.querySelectorAll("[data-action='copy']").forEach(function (button) {
      button.addEventListener("click", function () {
        if (!output.value) {
          generateInto(panel);
        }
        copy(output.value, button);
      });
    });

    // Typing your own identifier is a first-class path: someone with an
    // existing device should be able to fill the examples in without accepting
    // a generated value.
    output.addEventListener("input", function () {
      if (output.value) {
        fillExamples(panel.dataset.generator, output.value);
      }
    });
  }

  var panels = document.querySelectorAll("[data-generator]");
  panels.forEach(wirePanel);

  // One button that fills both, because the two are always needed together.
  document.querySelectorAll("[data-action='generate-all']").forEach(function (button) {
    button.addEventListener("click", function () {
      panels.forEach(generateInto);
      announce(button, "Generated");
    });
  });

  // Copy a whole example, with whatever has been substituted into it.
  document.querySelectorAll("[data-action='copy-code']").forEach(function (button) {
    button.addEventListener("click", function () {
      var block = button.closest(".code-block");
      if (block) {
        copy(block.querySelector("code").innerText, button);
      }
    });
  });
})();
