// Plays the first saved chunk through rodio with the same code path
// the companion-tauri audio worker uses. If we hear it here but not in
// the live app, the per-turn sink lifecycle is the bug; if we don't
// hear it here either, rodio can't decode SBV2's WAV output.
//
// Run from the apps/companion-tauri dir:
//   cargo run --release --example probe_chunk_decode --features custom-protocol -- ../../tts_samples/_live_chunk_2.wav

fn main() {
    let path = std::env::args()
        .nth(1)
        .expect("usage: probe_chunk_decode <wav-path>");
    let bytes = std::fs::read(&path).expect("read wav");
    println!("read {} bytes from {}", bytes.len(), path);

    let (_stream, handle) = rodio::OutputStream::try_default().expect("open output");
    let sink = rodio::Sink::try_new(&handle).expect("new sink");

    let cursor = std::io::Cursor::new(bytes);
    match rodio::Decoder::new_wav(cursor) {
        Ok(source) => {
            println!("decoded ok; appending to sink");
            sink.append(source);
        }
        Err(e) => {
            eprintln!("rodio::Decoder::new_wav FAILED: {e}");
            std::process::exit(2);
        }
    }
    println!("sink.len()={} — sleeping until end", sink.len());
    sink.sleep_until_end();
    println!("done");
}
