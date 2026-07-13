// src/unflincher/static/js/report.js -- life-report interactions (Task 7). Loaded only by
// report.html via {% block scripts %}, after the shared app.js globals (streamInto) are defined.
// Builds a heading-derived table of contents and streams report generation into #report-stream.
const REPORT_HEADING_SELECTOR = "h2, h3, h4";

function describeReportHeadings(headings) {
  return headings.map((heading, index) => ({
    id: heading.id || `report-section-${index + 1}`,
    label: heading.textContent.trim(),
  }));
}

function initReportPage(doc = document) {
  const body = doc.getElementById("report-body");
  const toc = doc.getElementById("report-toc");
  if (body && toc) {
    const headings = [...body.querySelectorAll(REPORT_HEADING_SELECTOR)];
    const items = describeReportHeadings(headings);
    items.forEach((item, index) => {
      headings[index].id = item.id;
      const link = doc.createElement("a");
      link.href = `#${item.id}`;
      link.textContent = item.label;
      toc.append(link);
    });
    toc.hidden = items.length === 0;
  }

  const trigger = doc.getElementById("run-report");
  trigger?.addEventListener("click", async () => {
    const target = doc.getElementById("report-stream");
    trigger.disabled = true;
    await streamInto("/report/generate", null, target, () => window.location.reload(), () => {
      trigger.disabled = false;
    });
  });
}
if (typeof document !== "undefined") {
  document.addEventListener("DOMContentLoaded", () => initReportPage(document));
}
if (typeof module !== "undefined" && module.exports) {
  module.exports = {REPORT_HEADING_SELECTOR, describeReportHeadings, initReportPage};
}
