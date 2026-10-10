.pragma library

// Pure helpers behind the Cài đặt filter field (FR-4.2). No QML context
// lookups and no qsTr: callers pass the already-translated texts, so this
// file evaluates in a bare QJSEngine.

/// Lower-case, accent-free form of `text`: "Tốc độ đọc" → "toc do doc", so
/// a user typing without Vietnamese diacritics still finds the row.
function normalize(text) {
    if (text === undefined || text === null)
        return "";
    return String(text)
        .normalize("NFD")
        .replace(/[̀-ͯ]/g, "")
        .replace(/[đĐ]/g, "d")
        .toLowerCase()
        .replace(/\s+/g, " ")
        .trim();
}

/// True when `filter` is blank, or when every word of it occurs in one of
/// `texts` (a row's label, description and keywords).
function matches(filter, texts) {
    const needle = normalize(filter);
    if (needle === "")
        return true;
    const haystack = normalize((texts || []).join(" "));
    const words = needle.split(" ");
    for (let i = 0; i < words.length; i++)
        if (haystack.indexOf(words[i]) < 0)
            return false;
    return true;
}
