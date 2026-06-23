(function () {
  function makeResizable(table) {
    var headers = table.querySelectorAll("th");
    if (headers.length === 0) return;

    headers.forEach(function (th) {
      th.style.position = "relative";

      var handle = document.createElement("div");
      handle.style.position = "absolute";
      handle.style.top = "0";
      handle.style.right = "0";
      handle.style.width = "6px";
      handle.style.height = "100%";
      handle.style.cursor = "col-resize";
      handle.style.userSelect = "none";
      handle.style.zIndex = "10";
      handle.className = "resize-handle";

      th.appendChild(handle);

      var startX, startWidth, startTableWidth;

      handle.addEventListener("mousedown", function (e) {
        startX = e.pageX;
        startWidth = th.offsetWidth;
        startTableWidth = table.offsetWidth;

        // Set explicit widths on all headers so they don't jump/collapse
        var siblingHeaders = Array.from(headers);
        var siblingWidths = siblingHeaders.map(h => h.offsetWidth);
        siblingHeaders.forEach(function (h, idx) {
          h.style.width = siblingWidths[idx] + "px";
        });

        table.style.width = startTableWidth + "px";
        handle.style.borderRight = "2px solid var(--accent)";

        function onMouseMove(e) {
          var diff = e.pageX - startX;
          var width = startWidth + diff;
          if (width > 40) {
            th.style.width = width + "px";
            table.style.width = (startTableWidth + diff) + "px";
          }
        }

        function onMouseUp() {
          handle.style.borderRight = "";
          document.removeEventListener("mousemove", onMouseMove);
          document.removeEventListener("mouseup", onMouseUp);
        }

        document.addEventListener("mousemove", onMouseMove);
        document.addEventListener("mouseup", onMouseUp);
        e.preventDefault();
      });

      // Highlight handle on hover
      handle.addEventListener("mouseenter", function () {
        handle.style.borderRight = "2px solid var(--line)";
      });
      handle.addEventListener("mouseleave", function () {
        handle.style.borderRight = "";
      });
    });
  }

  // Run on all tables currently in document
  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll("table").forEach(function (table) {
      // 1. Make the table resizable
      makeResizable(table);

      // 2. Wrap in responsive container unless it already has one
      var parent = table.parentElement;
      if (!parent.classList.contains("table-responsive") && parent.style.overflowX !== "auto") {
        var wrapper = document.createElement("div");
        wrapper.className = "table-responsive";
        parent.insertBefore(wrapper, table);
        wrapper.appendChild(table);
      }
    });
  });
})();
