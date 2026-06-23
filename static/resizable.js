(function () {
  function makeResizable(table) {
    var headers = table.querySelectorAll("th");
    if (headers.length === 0) return;

    // Ensure fluid layout initially
    table.style.width = "100%";

    headers.forEach(function (th, index) {
      // The last column does not need a resize handle on its right edge
      if (index === headers.length - 1) return;

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

      var startX, siblingHeaders, siblingWidths;

      handle.addEventListener("mousedown", function (e) {
        startX = e.pageX;
        siblingHeaders = Array.from(headers);
        siblingWidths = siblingHeaders.map(h => h.offsetWidth);

        // Lock table and columns to exact pixels during dragging
        table.style.tableLayout = "fixed";
        table.style.width = table.offsetWidth + "px";
        siblingHeaders.forEach(function (h, idx) {
          h.style.width = siblingWidths[idx] + "px";
        });

        handle.style.borderRight = "2px solid var(--accent)";

        var nextTh = th.nextElementSibling;
        var thIdx = index;
        var nextIdx = index + 1;

        function onMouseMove(e) {
          var diff = e.pageX - startX;
          var newThWidth = siblingWidths[thIdx] + diff;
          var newNextWidth = siblingWidths[nextIdx] - diff;

          if (newThWidth > 40 && newNextWidth > 40) {
            th.style.width = newThWidth + "px";
            if (nextTh) {
              nextTh.style.width = newNextWidth + "px";
            }
          }
        }

        function onMouseUp() {
          handle.style.borderRight = "";
          document.removeEventListener("mousemove", onMouseMove);
          document.removeEventListener("mouseup", onMouseUp);

          // Convert all column widths to percentages so layout remains 100% fluid/responsive
          var currentWidths = siblingHeaders.map(h => h.offsetWidth);
          var total = currentWidths.reduce((a, b) => a + b, 0);
          siblingHeaders.forEach(function (h, idx) {
            h.style.width = ((currentWidths[idx] / total) * 100) + "%";
          });
          table.style.width = "100%";
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
