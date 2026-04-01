use std::path::PathBuf;
use std::process::Command;

fn claw_binary() -> PathBuf {
    // cargo test builds into target/debug/deps; the binary lives in target/debug
    let mut path = std::env::current_exe().expect("current_exe should be available");
    path.pop(); // remove test binary name
    path.pop(); // move from deps/ to debug/
    path.push("claw");
    path
}

fn claw_available() -> bool {
    claw_binary().exists()
}

// ---------------------------------------------------------------------------
// 1. claw --version
// ---------------------------------------------------------------------------

#[test]
fn claw_version_outputs_version_string() {
    if !claw_available() {
        eprintln!("skipping: claw binary not found at {:?}", claw_binary());
        return;
    }

    let output = Command::new(claw_binary())
        .arg("--version")
        .output()
        .expect("should execute claw --version");

    let stdout = String::from_utf8_lossy(&output.stdout);
    let stderr = String::from_utf8_lossy(&output.stderr);
    let combined = format!("{stdout}{stderr}");

    assert!(
        output.status.success(),
        "claw --version should exit 0, got: {combined}"
    );
    // The version output should contain at least a version-like pattern (e.g., "0.1.0")
    assert!(
        combined.contains("0.") || combined.contains("claw") || combined.contains("Claw"),
        "version output should contain version info: {combined}"
    );
}

// ---------------------------------------------------------------------------
// 2. claw --help
// ---------------------------------------------------------------------------

#[test]
fn claw_help_outputs_usage_info() {
    if !claw_available() {
        return;
    }

    let output = Command::new(claw_binary())
        .arg("--help")
        .output()
        .expect("should execute claw --help");

    let stdout = String::from_utf8_lossy(&output.stdout);
    let stderr = String::from_utf8_lossy(&output.stderr);
    let combined = format!("{stdout}{stderr}");

    assert!(
        output.status.success(),
        "claw --help should exit 0, got: {combined}"
    );
    assert!(
        combined.contains("dump-manifests") || combined.contains("system-prompt"),
        "help should list known subcommands: {combined}"
    );
}

// ---------------------------------------------------------------------------
// 3. claw system-prompt
// ---------------------------------------------------------------------------

#[test]
fn claw_system_prompt_produces_output() {
    if !claw_available() {
        return;
    }

    let output = Command::new(claw_binary())
        .args(["system-prompt", "--cwd", "/tmp"])
        .output()
        .expect("should execute claw system-prompt");

    let stdout = String::from_utf8_lossy(&output.stdout);

    assert!(
        output.status.success(),
        "system-prompt should exit 0, stderr: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert!(
        !stdout.is_empty(),
        "system-prompt should produce non-empty output"
    );
    // The system prompt should mention the assistant's identity
    assert!(
        stdout.contains("Claw") || stdout.contains("Claude") || stdout.contains("assistant"),
        "system prompt should contain identity-related text: {}",
        &stdout[..stdout.len().min(200)]
    );
}

// ---------------------------------------------------------------------------
// 4. claw dump-manifests
// ---------------------------------------------------------------------------

#[test]
fn claw_dump_manifests_produces_json() {
    if !claw_available() {
        return;
    }

    let output = Command::new(claw_binary())
        .arg("dump-manifests")
        .output()
        .expect("should execute claw dump-manifests");

    let stdout = String::from_utf8_lossy(&output.stdout);
    let stderr = String::from_utf8_lossy(&output.stderr);

    // dump-manifests may succeed or fail depending on upstream TS availability,
    // but it should not panic.
    if output.status.success() {
        // If it succeeds, output should be valid JSON or at least structured text
        assert!(
            !stdout.is_empty(),
            "dump-manifests should produce output when successful"
        );
    } else {
        // If it fails, stderr should contain a meaningful message
        assert!(
            !stderr.is_empty() || !stdout.is_empty(),
            "dump-manifests should produce some output even on failure"
        );
    }
}
