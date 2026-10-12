use serde::Serialize;
use std::io::{Read, Seek, SeekFrom};
use std::path::Path;

#[derive(Serialize)]
pub struct InstallProgress {
    pub completed: u8,
    pub label: String,
    pub log: String,
}

fn parse_stage(text: &str) -> (u8, String) {
    text.trim()
        .split_once('|')
        .and_then(|(value, label)| {
            value
                .parse::<u8>()
                .ok()
                .map(|value| (value.min(8), label.to_string()))
        })
        .unwrap_or((0, "Đang chuẩn bị bộ cài…".into()))
}

pub fn snapshot(target: &Path) -> Result<InstallProgress, String> {
    let stage = std::fs::read_to_string(target.join("logs/setup-progress")).unwrap_or_default();
    let (completed, label) = parse_stage(&stage);
    let log_path = target.join("logs/setup.log");
    let log = if log_path.exists() {
        let mut file = std::fs::File::open(log_path).map_err(|e| e.to_string())?;
        let size = file.metadata().map_err(|e| e.to_string())?.len();
        file.seek(SeekFrom::Start(size.saturating_sub(64 * 1024)))
            .map_err(|e| e.to_string())?;
        let mut bytes = Vec::new();
        file.take(64 * 1024)
            .read_to_end(&mut bytes)
            .map_err(|e| e.to_string())?;
        String::from_utf8_lossy(&bytes).into_owned()
    } else {
        String::new()
    };
    Ok(InstallProgress {
        completed,
        label,
        log,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn stages_are_bounded_and_incomplete_writes_are_safe() {
        assert_eq!(parse_stage("6|Tải model\n"), (6, "Tải model".into()));
        assert_eq!(parse_stage("99|Done").0, 8);
        assert_eq!(parse_stage("partial").0, 0);
        assert_eq!(parse_stage("oops|bad").0, 0);
    }
    #[test]
    fn log_reads_only_a_bounded_tail() {
        let target =
            std::env::temp_dir().join(format!("render-progress-test-{}", std::process::id()));
        std::fs::create_dir_all(target.join("logs")).unwrap();
        std::fs::write(
            target.join("logs/setup.log"),
            "x".repeat(100_000) + "FINISH",
        )
        .unwrap();
        let result = snapshot(&target).unwrap();
        assert_eq!(result.log.len(), 64 * 1024);
        assert!(result.log.ends_with("FINISH"));
        std::fs::remove_dir_all(target).unwrap();
    }
}
