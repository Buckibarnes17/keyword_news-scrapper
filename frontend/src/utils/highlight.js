export function escapeHtml(text) {
  if (!text) return '';
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

export function escapeRegExp(string) {
  return string.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

// Simple extractor for search highlight phrases
export function extractHighlightTerms(query) {
  if (!query) return [];
  // Find terms in quotes or plain alphabetic terms
  const matches = query.match(/"([^"]+)"|(\b\w+\b)/g) || [];
  return matches
    .map(m => m.replace(/"/g, '').trim())
    .filter(m => m.toUpperCase() !== 'AND' && m.toUpperCase() !== 'OR' && m.toUpperCase() !== 'NOT');
}
