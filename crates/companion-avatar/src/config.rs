//! Avatar config types — owned by companion-avatar, deserialized from
//! the `[avatar]` table in `companion.toml`.

use std::collections::HashMap;

use serde::{Deserialize, Serialize};

use companion_core::llm::LlmConfig;

/// Top-level avatar configuration.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AvatarConfig {
    #[serde(default)]
    pub enabled: bool,
    /// Language the user chats with the agent in. Subtitles use this language;
    /// when it differs from `tts.language` the subagent translates each reply
    /// before TTS synthesis.
    #[serde(default = "default_chat_language")]
    pub chat_language: String,
    #[serde(default)]
    pub tts: AvatarTtsConfig,
    #[serde(default)]
    pub model: Live2DModelConfig,
    #[serde(default)]
    pub expressions: ExpressionMappingConfig,
    #[serde(default)]
    pub lip_sync: LipSyncConfig,
    #[serde(default)]
    pub subagent: AvatarSubagentConfig,
}

impl Default for AvatarConfig {
    fn default() -> Self {
        Self {
            enabled: false,
            chat_language: default_chat_language(),
            tts: AvatarTtsConfig::default(),
            model: Live2DModelConfig::default(),
            expressions: ExpressionMappingConfig::default(),
            lip_sync: LipSyncConfig::default(),
            subagent: AvatarSubagentConfig::default(),
        }
    }
}

fn default_chat_language() -> String {
    "en".into()
}

/// Split text into TTS-friendly chunks.
///
/// Each chunk is one or more **whole sentences** — we never break
/// inside a sentence. `target` is a *soft* target chunk length in
/// chars: whole sentences get packed into the current chunk until it
/// reaches `target`, then a new one starts. Hard cap is `2 × target`;
/// a single sentence longer than the cap is sub-split at the last
/// comma (`,` / `、`) before the cap, never mid-word.
///
/// Sentence detection (in `raw_sentences`) treats `。！？!?` and bare
/// newlines as ends, but is conservative about `.` — it does **not**
/// end a sentence on a decimal point (`3.14`), an ellipsis (`...`), or
/// a common abbreviation (`Mr.`, `e.g.`, `U.S.`, single initials).
/// That keeps the TTS from giving falling sentence-final intonation to
/// "...the answer is 3" and then starting "14 percent" cold.
pub fn split_sentences(text: &str, target: usize) -> Vec<String> {
    let target = target.max(16);
    let hard_cap = target.saturating_mul(2).max(target + 32);

    // 1. Raw sentences (each includes its terminator + trailing close
    //    quotes/parens). A sentence longer than the hard cap gets
    //    sub-split into ~`target`-sized pieces at commas/spaces.
    let mut sentences: Vec<String> = Vec::new();
    for s in raw_sentences(text) {
        if s.chars().count() <= hard_cap {
            sentences.push(s);
        } else {
            sentences.extend(soft_wrap_long(&s, target));
        }
    }

    // 2. Greedily pack whole sentences toward `target`.
    let mut out: Vec<String> = Vec::new();
    let mut cur = String::new();
    let mut cur_len: usize = 0;
    for s in sentences {
        let s_len = s.chars().count();
        if cur_len == 0 {
            cur = s;
            cur_len = s_len;
        } else if cur_len < target && cur_len + s_len <= hard_cap {
            if needs_space_between(&cur, &s) {
                cur.push(' ');
            }
            cur.push_str(&s);
            cur_len += s_len; // approx (ignores the optional space) — fine
        } else {
            out.push(std::mem::take(&mut cur));
            cur = s;
            cur_len = s_len;
        }
    }
    if !cur.is_empty() {
        // A tiny trailing chunk (< a third of target) folds into the
        // previous one so we don't ship "OK!" or "ね。" on its own —
        // unless that would push the previous chunk over the hard cap.
        let last_fits = out
            .last()
            .map(|l| l.chars().count() + cur_len <= hard_cap)
            .unwrap_or(false);
        if cur_len < target / 3 && last_fits {
            let last = out.last_mut().unwrap();
            if needs_space_between(last, &cur) {
                last.push(' ');
            }
            last.push_str(&cur);
        } else {
            out.push(cur);
        }
    }

    out.into_iter()
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty())
        .collect()
}

/// Common English abbreviations that end in `.` but don't end a
/// sentence. Lowercased; we strip a trailing `.` from the candidate
/// word before checking, so `e.g.` → `e.g` matches, `Mr.` → `mr`.
const ABBREVIATIONS: &[&str] = &[
    "mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "vs", "etc",
    "e.g", "i.e", "a.m", "p.m", "u.s", "u.k", "u.n", "no", "inc", "ltd",
    "co", "corp", "fig", "vol", "ch", "pp", "approx", "dept", "est",
    "min", "max", "esp", "cf", "al", // "et al."
];

/// True if `word` (the run of non-space chars ending right before a
/// `.`) is something we should NOT treat as a sentence end. Strips
/// leading/trailing punctuation first, so `(e.g.` → `e.g`, `"Mr.` →
/// `mr`, `J.` → `j`.
fn is_abbreviation(word: &str) -> bool {
    let w = word
        .trim_start_matches(|c: char| !c.is_alphanumeric())
        .trim_end_matches('.')
        .to_ascii_lowercase();
    if w.is_empty() {
        return false;
    }
    // A single letter ("J." in "J. R. R. Tolkien", "A." in a numbered
    // outline) — treat as an abbreviation/initial.
    if w.chars().count() == 1 && w.chars().next().unwrap().is_alphabetic() {
        return true;
    }
    ABBREVIATIONS.contains(&w.as_str())
}

/// Walk `text` and emit each sentence as a separate string, terminator
/// included. Conservative about `.` — see `split_sentences` doc.
fn raw_sentences(text: &str) -> Vec<String> {
    let chars: Vec<char> = text.chars().collect();
    let mut out: Vec<String> = Vec::new();
    let mut buf = String::new();
    let mut i = 0usize;
    while i < chars.len() {
        let ch = chars[i];
        buf.push(ch);
        let prev = if i > 0 { Some(chars[i - 1]) } else { None };
        let next = chars.get(i + 1).copied();

        let is_end = match ch {
            // CJK sentence-final punctuation: unambiguous.
            '。' | '！' | '？' => true,
            // ASCII `!` / `?` — practically always a sentence end.
            '!' | '?' => true,
            // A bare newline ends the current line/paragraph. Markdown
            // structure (`#`, `-`, `>`) is stripped downstream; what we
            // care about here is not gluing two paragraphs into one
            // breath.
            '\n' => true,
            '.' => {
                // Decimal point: digit . digit  → not a sentence end.
                let between_digits = prev.is_some_and(|c| c.is_ascii_digit())
                    && next.is_some_and(|c| c.is_ascii_digit());
                // Ellipsis: part of a `..`/`...` run → not an end on
                // its own (the run continues; a real end may follow).
                let in_ellipsis = prev == Some('.') || next == Some('.');
                if between_digits || in_ellipsis {
                    false
                } else {
                    // Abbreviation: look back to the start of the word
                    // that the `.` closes.
                    let word_start = buf
                        .char_indices()
                        .rev()
                        .find(|&(_, c)| c.is_whitespace())
                        .map(|(idx, c)| idx + c.len_utf8())
                        .unwrap_or(0);
                    let word = &buf[word_start..];
                    !is_abbreviation(word)
                }
            }
            _ => false,
        };

        i += 1;
        if is_end {
            // Pull in trailing close-quotes / parens / a following
            // closing CJK quote so they ride with the sentence they
            // close rather than orphaning at the next chunk's head.
            while let Some(&c) = chars.get(i) {
                if matches!(c, '"' | '\'' | ')' | ']' | '}' | '」' | '』' | '）' | '"') {
                    buf.push(c);
                    i += 1;
                } else {
                    break;
                }
            }
            let s = buf.trim();
            if !s.is_empty() {
                out.push(s.to_string());
            }
            buf.clear();
        }
    }
    let tail = buf.trim();
    if !tail.is_empty() {
        out.push(tail.to_string());
    }
    out
}

/// Sub-split a single over-long sentence at the last comma (ASCII `,`
/// or CJK `、`) before `cap` chars; if there's no comma in range, hard-
/// wrap at `cap` on a char boundary (never inside a char, never inside
/// a word for ASCII — backs up to the last space).
fn soft_wrap_long(sentence: &str, cap: usize) -> Vec<String> {
    let chars: Vec<char> = sentence.chars().collect();
    let mut out: Vec<String> = Vec::new();
    let mut start = 0usize;
    while chars.len() - start > cap {
        // Window [start, start+cap). Prefer a comma; else a space.
        let window_end = start + cap;
        let mut split_at = None;
        for j in (start + cap / 2..window_end).rev() {
            if matches!(chars[j], ',' | '、') {
                split_at = Some(j + 1); // include the comma
                break;
            }
        }
        if split_at.is_none() {
            for j in (start + cap / 2..window_end).rev() {
                if chars[j].is_whitespace() {
                    split_at = Some(j); // break before the space
                    break;
                }
            }
        }
        let end = split_at.unwrap_or(window_end);
        let piece: String = chars[start..end].iter().collect();
        let piece = piece.trim();
        if !piece.is_empty() {
            out.push(piece.to_string());
        }
        start = end;
    }
    let rest: String = chars[start..].iter().collect();
    let rest = rest.trim();
    if !rest.is_empty() {
        out.push(rest.to_string());
    }
    out
}

/// Whether to put a space between two chunks being joined. Latin text
/// uses inter-sentence spaces; CJK doesn't. Heuristic: space iff the
/// last char of `a` or the first char of `b` is ASCII.
fn needs_space_between(a: &str, b: &str) -> bool {
    let last = a.chars().last();
    let first = b.chars().next();
    let ascii_ish = |c: Option<char>| c.is_some_and(|c| c.is_ascii());
    ascii_ish(last) || ascii_ish(first)
}

#[cfg(test)]
mod tests_split {
    use super::*;

    const T: usize = 80; // production-ish target

    fn rejoin(v: &[String]) -> String {
        // Collapse runs of whitespace so the comparison ignores the
        // optional inter-chunk space the packer may insert.
        v.join(" ").split_whitespace().collect::<Vec<_>>().join(" ")
    }
    fn normspace(s: &str) -> String {
        s.split_whitespace().collect::<Vec<_>>().join(" ")
    }

    #[test]
    fn empty_input_yields_nothing() {
        assert!(split_sentences("", T).is_empty());
        assert!(split_sentences("   \n  ", T).is_empty());
    }

    #[test]
    fn no_terminator_returns_whole_text() {
        assert_eq!(split_sentences("just a phrase no period", T), vec!["just a phrase no period"]);
    }

    /// Round-trip: rejoining the chunks (modulo whitespace) must equal
    /// the input. We never drop or reorder text.
    #[test]
    fn order_and_content_preserved() {
        for input in [
            "First. Second. Third.",
            "こんにちは！アスナです。明日に向けてサポートするよ！あなたはどうですか？",
            "Mixed です。 And English. そして more 日本語。",
            "Try this. 1. First tip. 2. Second tip.",
        ] {
            let v = split_sentences(input, T);
            assert_eq!(rejoin(&v), normspace(input), "round-trip failed for {input:?}");
        }
    }

    /// Decimals are not sentence ends — "3.14" must not be cut.
    #[test]
    fn decimal_points_dont_split() {
        let v = split_sentences("Pi is roughly 3.14159 which is enough. Next thing.", T);
        assert!(v.iter().any(|c| c.contains("3.14159")), "decimal got split: {v:?}");
        assert!(!v.iter().any(|c| c.trim() == "14159 which is enough."), "split mid-decimal: {v:?}");
    }

    /// Common abbreviations don't end a sentence.
    #[test]
    fn abbreviations_dont_split() {
        // With a small target so packing wouldn't otherwise glue them.
        let v = split_sentences("Talk to Dr. Smith about it. Then call Mr. Jones.", 24);
        assert!(v.iter().any(|c| c.contains("Dr. Smith")), "Dr. got split: {v:?}");
        assert!(v.iter().any(|c| c.contains("Mr. Jones")), "Mr. got split: {v:?}");
        // "e.g." mid-sentence.
        let v2 = split_sentences("Use a fast model, e.g. gpt-4o-mini, for this. Done.", 24);
        assert!(v2.iter().any(|c| c.contains("e.g. gpt-4o-mini")), "e.g. got split: {v2:?}");
    }

    /// Ellipsis doesn't fragment.
    #[test]
    fn ellipsis_doesnt_fragment() {
        let v = split_sentences("Hmm... maybe later. Sure.", 24);
        assert!(v.iter().any(|c| c.contains("Hmm... maybe later.")), "ellipsis fragmented: {v:?}");
    }

    /// No chunk exceeds the hard cap (2× target, min target+32).
    #[test]
    fn respects_hard_cap() {
        let long_sentence = "あ".repeat(500) + "。"; // a single 501-char sentence
        let cap = (T * 2).max(T + 32);
        for c in split_sentences(&long_sentence, T) {
            assert!(c.chars().count() <= cap, "chunk over cap: {} chars", c.chars().count());
        }
    }

    /// Whole-sentence boundaries: every chunk (except possibly the
    /// last, if the input had no trailing terminator) ends with a
    /// terminator or a close-quote following one — never mid-sentence.
    #[test]
    fn chunks_end_on_sentence_boundaries() {
        let v = split_sentences(
            "こんにちは！アスナです。明日に向けてサポートするよ！あなたはどうですか？それじゃ、頑張ろうね。",
            T,
        );
        for (i, c) in v.iter().enumerate() {
            let last_real = c.trim_end_matches(|ch: char| {
                matches!(ch, '"' | '\'' | ')' | ']' | '}' | '」' | '』' | '）')
            }).chars().last().unwrap();
            let ok = matches!(last_real, '.' | '!' | '?' | '。' | '！' | '？');
            assert!(ok || i + 1 == v.len(), "chunk {i} ends mid-sentence: {c:?}");
        }
    }

    /// Diagnostic dump — run with:
    ///   cargo test -p companion-avatar dump_chunks --release -- --nocapture
    #[test]
    fn dump_chunks() {
        let cases: &[(&str, &str)] = &[
            ("short-ja", "こんにちは！アスナです。"),
            ("medium-ja", "こんにちは！アスナです。明日に向けてサポートするよ！あなたはどうですか？"),
            ("long-ja",
             "こんにちは！アスナです！ゲームでレベルを上げる時でも、実際の試験に備える時でも、本当に役立つ勉強のコツを3つご紹介します。1つ目はポモドーロテクニック。25分集中して5分休憩を繰り返します。2つ目はアクティブリコール。学んだことを思い出す練習をしましょう。3つ目は十分な睡眠です。記憶の定着には睡眠がとても大切なんだよ。"),
            ("long-en",
             "Hey, welcome back — long day at work? I figured you'd be tired so I kept it low-key. \
              By the way, the answer to your question earlier is roughly 3.14159, give or take. \
              If you want to dig into it more, Dr. Smith's notes (e.g. the section on convergence) cover it well. \
              Anyway... want to watch something, or are you heading to bed? Either way I'm here."),
            ("run-on-en",
             "I think we should first set up the environment then install the dependencies then configure \
              the database connection then run the migrations then start the dev server and only after \
              all of that is working should we even think about writing the actual feature code because \
              otherwise we'll be debugging plumbing instead of logic and that's a waste of an evening."),
        ];
        for (name, text) in cases {
            let v = split_sentences(text, 80);
            eprintln!("\n=== {name} (input {}c → {} chunk(s)) ===", text.chars().count(), v.len());
            for (i, c) in v.iter().enumerate() {
                eprintln!("  [{i}] {:>3}c | {c}", c.chars().count());
            }
        }
    }
}

/// TTS port configuration.
///
/// The companion speaks a single, model-agnostic HTTP contract:
///
/// - `POST {api_url}/tts` JSON: `{"text", "language", "voice"?, "speed"?}`
///   → audio bytes (optional `X-Sample-Rate`, `X-Channels`, `X-Format`).
/// - `GET {api_url}/health` → 200 OK when ready.
///
/// Engine-specific knobs are forwarded to the spawned wrapper as env vars
/// (`TTS_MODEL_PATH`, `TTS_REFERENCE_AUDIO`, `TTS_REFERENCE_TEXT`,
/// `TTS_REFERENCE_LANG`, `TTS_VOICE`, `CUDA_VISIBLE_DEVICES`).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AvatarTtsConfig {
    #[serde(default = "default_tts_engine")]
    pub engine: String,
    #[serde(default)]
    pub api_url: Option<String>,
    #[serde(default)]
    pub model_path: Option<String>,
    #[serde(default)]
    pub reference_audio: Option<String>,
    #[serde(default)]
    pub reference_text: Option<String>,
    #[serde(default)]
    pub reference_language: Option<String>,
    #[serde(default = "default_gpu_device")]
    pub gpu_device: i32,
    #[serde(default)]
    pub port: u16,
    #[serde(default)]
    pub launch_command: Option<String>,
    #[serde(default = "default_true")]
    pub auto_start: bool,
    #[serde(default)]
    pub voice: Option<String>,
    #[serde(default = "default_tts_language")]
    pub language: String,
    #[serde(default = "default_tts_speed")]
    pub speed: f32,
    /// When true (default), split each agent reply into sentences and
    /// synthesize them in order, broadcasting each as its own Audio
    /// frame. The first chunk arrives ~1–2s after the agent reply
    /// instead of waiting for the full reply to render. Set false to
    /// fall back to single-shot synthesis (whole reply as one WAV).
    #[serde(default = "default_true")]
    pub streaming: bool,
    /// Target chunk length (in chars) for TTS streaming. The chunker
    /// packs *whole sentences* into chunks until they reach roughly
    /// this length, then starts a new one — never breaking inside a
    /// sentence. Larger → more natural prosody (the TTS model plans
    /// intonation across the whole chunk) but slower time-to-first-
    /// audio; smaller → snappier first audio but choppier. ~80 ≈ a
    /// couple of short sentences ≈ 4-6 s of speech, which is the
    /// sweet spot. A chunk never exceeds `2×` this; a single sentence
    /// longer than that is sub-split at commas (still never mid-word).
    #[serde(default = "default_streaming_min_chars", alias = "streaming_target_chars")]
    pub streaming_min_chars: usize,
}

fn default_streaming_min_chars() -> usize {
    // History: 8 (one-word chunks, clipped prosody) → 24 (still glued
    // 3-4 short sentences into one chunk because the old logic merged
    // forward until it crossed the threshold) → 80 (this). 80 chars
    // is ~a couple of short Japanese sentences; the chunker now packs
    // whole sentences *toward* this length rather than treating it as
    // a hard floor, so chunk boundaries always land at real sentence
    // ends. Override in companion.toml `[avatar.tts] streaming_min_chars`
    // — lower for snappier first audio, higher for fewer/longer chunks.
    80
}

fn default_tts_engine() -> String {
    "edge-tts".into()
}
fn default_gpu_device() -> i32 {
    0
}
fn default_tts_language() -> String {
    "en".into()
}
fn default_tts_speed() -> f32 {
    1.0
}
fn default_true() -> bool {
    true
}

impl Default for AvatarTtsConfig {
    fn default() -> Self {
        Self {
            engine: default_tts_engine(),
            api_url: None,
            model_path: None,
            reference_audio: None,
            reference_text: None,
            reference_language: None,
            gpu_device: default_gpu_device(),
            port: 9880,
            launch_command: None,
            auto_start: true,
            voice: None,
            language: default_tts_language(),
            speed: default_tts_speed(),
            streaming: true,
            streaming_min_chars: default_streaming_min_chars(),
        }
    }
}

/// Live2D model configuration.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Live2DModelConfig {
    #[serde(default)]
    pub model_dir: Option<String>,
    #[serde(default = "default_avatar_expression")]
    pub default_expression: String,
    #[serde(default = "default_model_scale")]
    pub scale: f32,
    #[serde(default = "default_model_anchor")]
    pub anchor: String,
}

fn default_avatar_expression() -> String {
    "neutral".into()
}
fn default_model_scale() -> f32 {
    0.2
}
fn default_model_anchor() -> String {
    "center".into()
}

impl Default for Live2DModelConfig {
    fn default() -> Self {
        Self {
            model_dir: None,
            default_expression: default_avatar_expression(),
            scale: default_model_scale(),
            anchor: default_model_anchor(),
        }
    }
}

/// Expression mapping from agent emotions to Live2D expressions.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ExpressionMappingConfig {
    #[serde(default)]
    pub mapping: HashMap<String, String>,
    #[serde(default = "default_avatar_expression")]
    pub default: String,
    #[serde(default = "default_emotion_detection")]
    pub detection_mode: String,
    #[serde(default)]
    pub keyword_map: HashMap<String, String>,
}

fn default_emotion_detection() -> String {
    "keyword".into()
}

impl Default for ExpressionMappingConfig {
    fn default() -> Self {
        Self {
            mapping: HashMap::from([
                ("happy".to_string(), "smile".to_string()),
                ("sad".to_string(), "depressed".to_string()),
                ("angry".to_string(), "angry".to_string()),
                ("surprised".to_string(), "surprised".to_string()),
            ]),
            default: default_avatar_expression(),
            detection_mode: default_emotion_detection(),
            keyword_map: HashMap::from([
                ("happy".to_string(), "happy".to_string()),
                ("glad".to_string(), "happy".to_string()),
                ("sad".to_string(), "sad".to_string()),
                ("sorry".to_string(), "sad".to_string()),
                ("angry".to_string(), "angry".to_string()),
                ("wow".to_string(), "surprised".to_string()),
                ("surprised".to_string(), "surprised".to_string()),
            ]),
        }
    }
}

/// Lip sync configuration.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct LipSyncConfig {
    #[serde(default = "default_lip_sync_method")]
    pub method: String,
    #[serde(default = "default_lip_sync_smoothing")]
    pub smoothing: f32,
    #[serde(default = "default_mouth_open_param")]
    pub mouth_open_param: String,
    #[serde(default = "default_mouth_smile_param")]
    pub mouth_smile_param: String,
    #[serde(default = "default_lip_sync_fps")]
    pub fps: u32,
}

fn default_lip_sync_method() -> String {
    "volume".into()
}
fn default_lip_sync_smoothing() -> f32 {
    0.3
}
fn default_mouth_open_param() -> String {
    "ParamMouthOpenY".into()
}
fn default_mouth_smile_param() -> String {
    "ParamMouthSmile".into()
}
fn default_lip_sync_fps() -> u32 {
    30
}

impl Default for LipSyncConfig {
    fn default() -> Self {
        Self {
            method: default_lip_sync_method(),
            smoothing: default_lip_sync_smoothing(),
            mouth_open_param: default_mouth_open_param(),
            mouth_smile_param: default_mouth_smile_param(),
            fps: default_lip_sync_fps(),
        }
    }
}

/// Avatar subagent: a cheap LLM call that emits expression JSON and (when
/// `chat_language ≠ tts.language`) a translated reply.
///
/// Two backends:
/// - `llm` (default): direct OpenAI-compatible call. Fastest. Requires
///   a plaintext API key in this config (or via env var).
/// - `use_zeroclaw_webhook = true`: re-uses upstream zeroclaw as the LLM
///   by POSTing to its `/webhook`. No plaintext key needed in companion —
///   zeroclaw already has its keys decrypted. Slower (each agent reply
///   triggers a second zeroclaw round trip), but very simple to set up.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AvatarSubagentConfig {
    #[serde(default)]
    pub enabled: bool,
    /// When `true` (default), only run the subagent when chat_language
    /// differs from tts.language — i.e. when we actually need
    /// translation. For same-language setups this skips a 5-10s LLM
    /// call and falls back to fast keyword-based expression detection.
    /// Set to `false` if you want the LLM to always pick richer
    /// expressions even when no translation is needed.
    #[serde(default = "default_true")]
    pub only_when_translating: bool,
    /// When `true`, route subagent calls through the configured zeroclaw
    /// daemon (via `[zeroclaw] url`) instead of a direct LLM endpoint.
    /// Reuses zeroclaw's keys; no plaintext key needed below.
    #[serde(default)]
    pub use_zeroclaw_webhook: bool,
    /// When `true`, stream the translation token-by-token: TTS starts
    /// on the first complete sentence ~3s after the LLM begins,
    /// instead of waiting ~15-25s for a bulk JSON analyze() to finish.
    /// Trade-off: skips LLM-driven expression in favor of keyword
    /// matching (fast and good enough for most replies). Only meaningful
    /// when `use_zeroclaw_webhook = false` — webhook backend has no
    /// streaming surface.
    #[serde(default)]
    pub streaming: bool,
    /// LLM endpoint + model. Use any OpenAI-compatible provider
    /// (OpenAI, OpenRouter, Together, Groq, Ollama, vLLM, …). Ignored
    /// when `use_zeroclaw_webhook = true`.
    #[serde(default)]
    pub llm: LlmConfig,
    /// Custom system prompt override (replaces the built-in default).
    /// Supports `{chat_lang}` / `{tts_lang}` placeholders.
    #[serde(default)]
    pub system_prompt: Option<String>,
    /// Per-call timeout in seconds.
    #[serde(default = "default_subagent_timeout")]
    pub timeout_secs: u64,
}

fn default_subagent_timeout() -> u64 {
    3
}

impl Default for AvatarSubagentConfig {
    fn default() -> Self {
        Self {
            enabled: false,
            only_when_translating: true,
            use_zeroclaw_webhook: false,
            streaming: false,
            llm: LlmConfig::default(),
            system_prompt: None,
            timeout_secs: default_subagent_timeout(),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_minimal_avatar_toml() {
        let toml = r#"
            enabled = true
            chat_language = "en"
            [tts]
            language = "ja"
            engine = "gpt-sovits-v4"
        "#;
        let cfg: AvatarConfig = toml::from_str(toml).unwrap();
        assert!(cfg.enabled);
        assert_eq!(cfg.chat_language, "en");
        assert_eq!(cfg.tts.language, "ja");
        assert_eq!(cfg.tts.engine, "gpt-sovits-v4");
    }
}
