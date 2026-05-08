//! Audio-driven lip sync: extracts mouth parameter data from audio bytes.
//!
//! Supports volume-based (RMS amplitude) lip sync. Analyzes audio at a
//! configurable frame rate and produces a time series of mouth parameters.

use crate::config::LipSyncConfig;

/// Lip sync data for a complete audio segment.
#[derive(Debug, Clone)]
pub struct LipSyncData {
    /// Individual lip sync frames.
    pub frames: Vec<LipSyncFrame>,
    /// Audio sample rate.
    pub sample_rate: u32,
    /// Duration of each analysis frame in milliseconds.
    pub frame_duration_ms: u32,
}

/// A single lip sync frame at a point in time.
#[derive(Debug, Clone)]
pub struct LipSyncFrame {
    /// Timestamp in milliseconds from audio start.
    pub timestamp_ms: u32,
    /// Mouth open amount 0.0–1.0.
    pub mouth_open: f32,
    /// Mouth smile amount -1.0–1.0 (currently always 0.0 for volume-based).
    pub mouth_smile: f32,
}

/// Analyzes audio to produce lip sync parameter data.
pub struct LipSyncAnalyzer {
    /// Smoothing factor (0.0–1.0). Higher = more smoothing.
    smoothing: f32,
    /// Mouth open parameter name (for reference, not used in analysis).
    mouth_open_param: String,
    /// Analysis frame rate in Hz.
    fps: u32,
}

impl LipSyncAnalyzer {
    /// Build from config.
    pub fn new(config: &LipSyncConfig) -> Self {
        Self {
            smoothing: config.smoothing,
            mouth_open_param: config.mouth_open_param.clone(),
            fps: config.fps,
        }
    }

    /// Analyze raw PCM audio data to extract lip sync frames.
    ///
    /// `audio` is expected to be 16-bit mono PCM at the given sample rate.
    /// If the audio format is different, the caller should convert first.
    pub fn analyze(&self, audio: &super::tts_server::AudioOutput) -> LipSyncData {
        let frame_duration_ms = 1000 / self.fps.max(1);
        let samples_per_frame =
            ((audio.sample_rate as u64 * u64::from(frame_duration_ms)) / 1000) as usize;

        let pcm_samples = self.decode_pcm(&audio.audio_bytes, &audio.format);

        let mut frames = Vec::new();
        let mut prev_value = 0.0_f32;
        let num_frames = pcm_samples.len() / samples_per_frame.max(1);

        for i in 0..num_frames {
            let start = i * samples_per_frame;
            let end = (start + samples_per_frame).min(pcm_samples.len());
            let frame_samples = &pcm_samples[start..end];

            if frame_samples.is_empty() {
                continue;
            }

            // Compute RMS amplitude
            let rms = compute_rms(frame_samples);

            // Normalize to 0.0–1.0 range (typical speech RMS is 0.01–0.3 for 16-bit)
            let raw_open = (rms * 5.0).min(1.0);

            // Apply exponential smoothing
            let smoothed = if self.smoothing > 0.0 {
                prev_value * self.smoothing + raw_open * (1.0 - self.smoothing)
            } else {
                raw_open
            };
            prev_value = smoothed;

            frames.push(LipSyncFrame {
                timestamp_ms: (i as u32) * frame_duration_ms,
                mouth_open: smoothed,
                mouth_smile: 0.0,
            });
        }

        // Ensure at least one frame
        if frames.is_empty() {
            frames.push(LipSyncFrame {
                timestamp_ms: 0,
                mouth_open: 0.0,
                mouth_smile: 0.0,
            });
        }

        LipSyncData {
            frames,
            sample_rate: audio.sample_rate,
            frame_duration_ms,
        }
    }

    /// Decode audio bytes to normalized f32 samples.
    ///
    /// For WAV format, skips the 44-byte header and interprets as 16-bit PCM.
    /// For raw PCM, interprets directly as 16-bit samples.
    /// For MP3, returns a single silent frame (actual decoding would need a library).
    fn decode_pcm(&self, bytes: &[u8], format: &str) -> Vec<f32> {
        let pcm_data = match format {
            "wav" => {
                // Skip WAV header (at least 44 bytes for standard PCM WAV)
                if bytes.len() > 44 {
                    &bytes[44..]
                } else {
                    return Vec::new();
                }
            }
            "pcm" => bytes,
            // For formats we can't decode simply, return empty
            // (in production, use a proper decoder or require WAV output from TTS)
            _ => return Vec::new(),
        };

        // Decode 16-bit little-endian PCM to f32 normalized [-1.0, 1.0]
        pcm_data
            .chunks_exact(2)
            .map(|chunk| {
                let sample = i16::from_le_bytes([chunk[0], chunk[1]]);
                f32::from(sample) / 32768.0
            })
            .collect()
    }
}

/// Compute RMS (root mean square) amplitude of audio samples.
fn compute_rms(samples: &[f32]) -> f32 {
    if samples.is_empty() {
        return 0.0;
    }
    let sum_sq: f32 = samples.iter().map(|s| s * s).sum();
    (sum_sq / samples.len() as f32).sqrt()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn default_config() -> LipSyncConfig {
        LipSyncConfig {
            method: "volume".to_string(),
            smoothing: 0.3,
            mouth_open_param: "ParamMouthOpenY".to_string(),
            mouth_smile_param: "ParamMouthSmile".to_string(),
            fps: 30,
        }
    }

    fn make_wav_audio(samples: &[i16]) -> Vec<u8> {
        let mut wav = Vec::new();
        // Minimal WAV header (44 bytes)
        let data_len = (samples.len() * 2) as u32;
        let file_size = 36 + data_len;
        // RIFF header
        wav.extend_from_slice(b"RIFF");
        wav.extend_from_slice(&file_size.to_le_bytes());
        wav.extend_from_slice(b"WAVE");
        // fmt chunk
        wav.extend_from_slice(b"fmt ");
        wav.extend_from_slice(&16u32.to_le_bytes()); // chunk size
        wav.extend_from_slice(&1u16.to_le_bytes()); // PCM format
        wav.extend_from_slice(&1u16.to_le_bytes()); // mono
        wav.extend_from_slice(&22050u32.to_le_bytes()); // sample rate
        wav.extend_from_slice(&44100u32.to_le_bytes()); // byte rate
        wav.extend_from_slice(&2u16.to_le_bytes()); // block align
        wav.extend_from_slice(&16u16.to_le_bytes()); // bits per sample
        // data chunk
        wav.extend_from_slice(b"data");
        wav.extend_from_slice(&data_len.to_le_bytes());
        for &s in samples {
            wav.extend_from_slice(&s.to_le_bytes());
        }
        wav
    }

    #[test]
    fn analyze_silence() {
        let config = default_config();
        let analyzer = LipSyncAnalyzer::new(&config);
        let silence: Vec<i16> = vec![0; 22050]; // 1 second of silence at 22050 Hz
        let audio = super::super::tts_server::AudioOutput {
            audio_bytes: make_wav_audio(&silence),
            sample_rate: 22050,
            channels: 1,
            format: "wav".to_string(),
        };
        let data = analyzer.analyze(&audio);
        // All frames should have near-zero mouth_open
        for frame in &data.frames {
            assert!(frame.mouth_open < 0.01, "expected silence, got {}", frame.mouth_open);
        }
    }

    #[test]
    fn analyze_loud_signal() {
        let config = default_config();
        let analyzer = LipSyncAnalyzer::new(&config);
        // Max amplitude sine-ish signal
        let loud: Vec<i16> = (0..22050).map(|i| {
            ((i as f32 * 0.1).sin() * 32000.0) as i16
        }).collect();
        let audio = super::super::tts_server::AudioOutput {
            audio_bytes: make_wav_audio(&loud),
            sample_rate: 22050,
            channels: 1,
            format: "wav".to_string(),
        };
        let data = analyzer.analyze(&audio);
        // At least some frames should have significant mouth_open
        let max_open = data.frames.iter().map(|f| f.mouth_open).fold(0.0_f32, f32::max);
        assert!(max_open > 0.3, "expected loud signal to open mouth, max was {max_open}");
    }

    #[test]
    fn compute_rms_empty() {
        assert_eq!(compute_rms(&[]), 0.0);
    }

    #[test]
    fn compute_rms_unit_signal() {
        let samples = [1.0_f32, -1.0, 1.0, -1.0];
        let rms = compute_rms(&samples);
        assert!((rms - 1.0).abs() < 0.001, "expected 1.0, got {rms}");
    }

    #[test]
    fn analyze_returns_at_least_one_frame_for_short_audio() {
        let config = default_config();
        let analyzer = LipSyncAnalyzer::new(&config);
        let tiny: Vec<i16> = vec![0; 10]; // way under one frame's window
        let audio = super::super::tts_server::AudioOutput {
            audio_bytes: make_wav_audio(&tiny),
            sample_rate: 22050,
            channels: 1,
            format: "wav".into(),
        };
        let data = analyzer.analyze(&audio);
        assert!(!data.frames.is_empty(), "must always emit at least one frame");
    }

    #[test]
    fn analyze_unsupported_format_returns_silent_frame() {
        let config = default_config();
        let analyzer = LipSyncAnalyzer::new(&config);
        let audio = super::super::tts_server::AudioOutput {
            audio_bytes: vec![1, 2, 3, 4, 5],
            sample_rate: 22050,
            channels: 1,
            format: "mp3".into(),
        };
        let data = analyzer.analyze(&audio);
        assert!(!data.frames.is_empty());
        assert!(data.frames[0].mouth_open < 0.01);
    }

    #[test]
    fn analyze_uses_configured_fps_for_frame_duration() {
        let mut config = default_config();
        config.fps = 60;
        let analyzer = LipSyncAnalyzer::new(&config);
        let samples: Vec<i16> = vec![0; 22050];
        let audio = super::super::tts_server::AudioOutput {
            audio_bytes: make_wav_audio(&samples),
            sample_rate: 22050,
            channels: 1,
            format: "wav".into(),
        };
        let data = analyzer.analyze(&audio);
        // 60 fps → ~16.66ms per frame, rounded down to 16
        assert!(
            (15..=17).contains(&data.frame_duration_ms),
            "expected ~16ms frame, got {}",
            data.frame_duration_ms
        );
    }

    #[test]
    fn smoothing_clamps_within_zero_one() {
        let config = default_config();
        let analyzer = LipSyncAnalyzer::new(&config);
        // Mix of huge and tiny samples → smoothed values should still
        // be within [0, 1] (mouth_open is RMS-based, can't exceed 1).
        let mixed: Vec<i16> = (0..22050)
            .map(|i| if i % 100 == 0 { i16::MAX } else { 0 })
            .collect();
        let audio = super::super::tts_server::AudioOutput {
            audio_bytes: make_wav_audio(&mixed),
            sample_rate: 22050,
            channels: 1,
            format: "wav".into(),
        };
        let data = analyzer.analyze(&audio);
        for f in &data.frames {
            assert!(
                (0.0..=1.0).contains(&f.mouth_open),
                "mouth_open {} out of [0,1]",
                f.mouth_open
            );
        }
    }
}
