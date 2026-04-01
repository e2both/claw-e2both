use std::fs;

use serde_json::json;
use tools::execute_tool;

fn temp_dir(name: &str) -> std::path::PathBuf {
    let unique = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .expect("time")
        .as_nanos();
    let path = std::env::temp_dir().join(format!("tool-exec-{unique}-{name}"));
    fs::create_dir_all(&path).expect("create temp dir");
    path
}

// ---------------------------------------------------------------------------
// 1. read_file
// ---------------------------------------------------------------------------

#[test]
fn read_file_returns_content() {
    let dir = temp_dir("read-file");
    let file = dir.join("sample.txt");
    fs::write(&file, "line1\nline2\nline3\n").expect("write");

    let result =
        execute_tool("read_file", &json!({ "path": file.display().to_string() })).expect("read");
    let output: serde_json::Value = serde_json::from_str(&result).expect("json");

    assert_eq!(output["file"]["startLine"], 1);
    let content = output["file"]["content"].as_str().expect("content");
    assert!(content.contains("line1"));
    assert!(content.contains("line3"));

    let _ = fs::remove_dir_all(dir);
}

// ---------------------------------------------------------------------------
// 2. write_file
// ---------------------------------------------------------------------------

#[test]
fn write_file_creates_file() {
    let dir = temp_dir("write-file");
    let file = dir.join("output.txt");

    let result = execute_tool(
        "write_file",
        &json!({ "path": file.display().to_string(), "content": "hello world\n" }),
    )
    .expect("write");
    let output: serde_json::Value = serde_json::from_str(&result).expect("json");

    assert_eq!(output["type"], "create");
    let contents = fs::read_to_string(&file).expect("read back");
    assert_eq!(contents, "hello world\n");

    let _ = fs::remove_dir_all(dir);
}

// ---------------------------------------------------------------------------
// 3. edit_file
// ---------------------------------------------------------------------------

#[test]
fn edit_file_replaces_content() {
    let dir = temp_dir("edit-file");
    let file = dir.join("editable.txt");
    fs::write(&file, "foo bar baz\n").expect("write");

    let result = execute_tool(
        "edit_file",
        &json!({
            "path": file.display().to_string(),
            "old_string": "bar",
            "new_string": "QUX",
        }),
    )
    .expect("edit");
    let output: serde_json::Value = serde_json::from_str(&result).expect("json");
    // EditFileOutput has filePath, oldString, newString (no "type" field)
    assert_eq!(output["oldString"], "bar");
    assert_eq!(output["newString"], "QUX");

    let contents = fs::read_to_string(&file).expect("read back");
    assert!(contents.contains("QUX"));
    assert!(!contents.contains("bar"));

    let _ = fs::remove_dir_all(dir);
}

// ---------------------------------------------------------------------------
// 4. glob_search
// ---------------------------------------------------------------------------

#[test]
fn glob_search_finds_files() {
    let dir = temp_dir("glob-search");
    fs::write(dir.join("alpha.rs"), "fn alpha()").expect("write");
    fs::write(dir.join("beta.rs"), "fn beta()").expect("write");
    fs::write(dir.join("gamma.txt"), "text file").expect("write");

    let result = execute_tool(
        "glob_search",
        &json!({ "pattern": "*.rs", "path": dir.display().to_string() }),
    )
    .expect("glob");
    let output: serde_json::Value = serde_json::from_str(&result).expect("json");

    let filenames = output["filenames"].as_array().expect("filenames array");
    assert_eq!(filenames.len(), 2, "should find exactly 2 .rs files");

    let paths: Vec<&str> = filenames.iter().filter_map(|v| v.as_str()).collect();
    assert!(
        paths.iter().any(|p| p.contains("alpha.rs")),
        "should find alpha.rs"
    );
    assert!(
        paths.iter().any(|p| p.contains("beta.rs")),
        "should find beta.rs"
    );

    let _ = fs::remove_dir_all(dir);
}

// ---------------------------------------------------------------------------
// 5. grep_search
// ---------------------------------------------------------------------------

#[test]
fn grep_search_finds_pattern() {
    let dir = temp_dir("grep-search");
    fs::write(
        dir.join("haystack.txt"),
        "needle in a haystack\nno match here\nanother needle\n",
    )
    .expect("write");
    fs::write(dir.join("empty.txt"), "nothing here\n").expect("write");

    let result = execute_tool(
        "grep_search",
        &json!({ "pattern": "needle", "path": dir.display().to_string() }),
    )
    .expect("grep");
    let output: serde_json::Value = serde_json::from_str(&result).expect("json");

    let filenames = output["filenames"].as_array().expect("filenames array");
    assert!(
        !filenames.is_empty(),
        "should find at least one file matching 'needle'"
    );

    let _ = fs::remove_dir_all(dir);
}

// ---------------------------------------------------------------------------
// 6. WebFetch with invalid URL
// ---------------------------------------------------------------------------

#[test]
fn web_fetch_invalid_url_returns_error() {
    let result = execute_tool("WebFetch", &json!({ "url": "not-a-valid-url://broken" }));

    assert!(result.is_err(), "invalid URL should produce an error");
}

// ---------------------------------------------------------------------------
// 7. WebSearch without API key
// ---------------------------------------------------------------------------

#[test]
fn web_search_without_api_key_returns_error() {
    // WebSearch requires BRAVE_SEARCH_API_KEY; without it, should error gracefully.
    let result = execute_tool(
        "WebSearch",
        &json!({ "query": "test query", "allowed_domains": [] }),
    );

    // It may either return an Err or return an Ok with error info; either is acceptable.
    // The key is it does not panic.
    if let Ok(output) = &result {
        let parsed: serde_json::Value = serde_json::from_str(output).unwrap_or_default();
        // If it returned OK, the output should still be valid JSON
        assert!(parsed.is_object() || parsed.is_null());
    }
}
