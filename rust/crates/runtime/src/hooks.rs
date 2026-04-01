use std::ffi::OsStr;
use std::process::Command;
use std::sync::mpsc;
use std::time::Duration;

use serde_json::json;

use crate::config::{HookEntry, HookEntryConfig, RuntimeFeatureConfig, RuntimeHookConfig};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum HookEvent {
    PreToolUse,
    PostToolUse,
    Notification,
}

impl HookEvent {
    fn as_str(self) -> &'static str {
        match self {
            Self::PreToolUse => "PreToolUse",
            Self::PostToolUse => "PostToolUse",
            Self::Notification => "Notification",
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct HookRunResult {
    denied: bool,
    messages: Vec<String>,
}

impl HookRunResult {
    #[must_use]
    pub fn allow(messages: Vec<String>) -> Self {
        Self {
            denied: false,
            messages,
        }
    }

    #[must_use]
    pub fn is_denied(&self) -> bool {
        self.denied
    }

    #[must_use]
    pub fn messages(&self) -> &[String] {
        &self.messages
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct HookRunner {
    pre_tool_use: Vec<HookEntry>,
    post_tool_use: Vec<HookEntry>,
    notification: Vec<HookEntry>,
}

impl HookRunner {
    #[must_use]
    #[allow(clippy::needless_pass_by_value)]
    pub fn new(config: RuntimeHookConfig) -> Self {
        Self {
            pre_tool_use: config
                .pre_tool_use()
                .iter()
                .cloned()
                .map(HookEntryConfig::into_entry)
                .collect(),
            post_tool_use: config
                .post_tool_use()
                .iter()
                .cloned()
                .map(HookEntryConfig::into_entry)
                .collect(),
            notification: config
                .notification()
                .iter()
                .cloned()
                .map(HookEntryConfig::into_entry)
                .collect(),
        }
    }

    #[must_use]
    pub fn from_feature_config(feature_config: &RuntimeFeatureConfig) -> Self {
        Self::new(feature_config.hooks().clone())
    }

    #[must_use]
    pub fn run_pre_tool_use(&self, tool_name: &str, tool_input: &str) -> HookRunResult {
        self.run_commands(
            HookEvent::PreToolUse,
            &self.pre_tool_use,
            tool_name,
            tool_input,
            None,
            false,
        )
    }

    #[must_use]
    pub fn run_post_tool_use(
        &self,
        tool_name: &str,
        tool_input: &str,
        tool_output: &str,
        is_error: bool,
    ) -> HookRunResult {
        self.run_commands(
            HookEvent::PostToolUse,
            &self.post_tool_use,
            tool_name,
            tool_input,
            Some(tool_output),
            is_error,
        )
    }

    #[must_use]
    pub fn run_notification(&self) -> HookRunResult {
        self.run_commands(
            HookEvent::Notification,
            &self.notification,
            "",
            "",
            None,
            false,
        )
    }

    fn run_commands(
        &self,
        event: HookEvent,
        entries: &[HookEntry],
        tool_name: &str,
        tool_input: &str,
        tool_output: Option<&str>,
        is_error: bool,
    ) -> HookRunResult {
        if entries.is_empty() {
            return HookRunResult::allow(Vec::new());
        }

        let payload = json!({
            "hook_event_name": event.as_str(),
            "tool_name": tool_name,
            "tool_input": parse_tool_input(tool_input),
            "tool_input_json": tool_input,
            "tool_output": tool_output,
            "tool_result_is_error": is_error,
        })
        .to_string();

        let mut messages = Vec::new();

        for entry in entries {
            if !should_run_for_tool(entry, tool_name) {
                continue;
            }

            match self.run_command(
                entry,
                event,
                tool_name,
                tool_input,
                tool_output,
                is_error,
                &payload,
            ) {
                HookCommandOutcome::Allow { message } => {
                    if let Some(message) = message {
                        messages.push(message);
                    }
                }
                HookCommandOutcome::Deny { message } => {
                    let message = message.unwrap_or_else(|| {
                        format!("{} hook denied tool `{tool_name}`", event.as_str())
                    });
                    messages.push(message);
                    return HookRunResult {
                        denied: true,
                        messages,
                    };
                }
                HookCommandOutcome::Warn { message } => messages.push(message),
            }
        }

        HookRunResult::allow(messages)
    }

    #[allow(clippy::too_many_arguments, clippy::unused_self)]
    fn run_command(
        &self,
        entry: &HookEntry,
        event: HookEvent,
        tool_name: &str,
        tool_input: &str,
        tool_output: Option<&str>,
        is_error: bool,
        payload: &str,
    ) -> HookCommandOutcome {
        let command = &entry.command;
        let mut child = shell_command(command);
        child.stdin(std::process::Stdio::piped());
        child.stdout(std::process::Stdio::piped());
        child.stderr(std::process::Stdio::piped());
        child.env("HOOK_EVENT", event.as_str());
        child.env("HOOK_TOOL_NAME", tool_name);
        child.env("HOOK_TOOL_INPUT", tool_input);
        child.env("HOOK_TOOL_IS_ERROR", if is_error { "1" } else { "0" });
        if let Some(tool_output) = tool_output {
            child.env("HOOK_TOOL_OUTPUT", tool_output);
        }

        if let Some(timeout_ms) = entry.timeout_ms {
            return self
                .run_command_with_timeout(child, command, event, tool_name, payload, timeout_ms);
        }

        match child.output_with_stdin(payload.as_bytes()) {
            Ok(output) => interpret_output(command, event, tool_name, &output),
            Err(error) => HookCommandOutcome::Warn {
                message: format!(
                    "{} hook `{command}` failed to start for `{tool_name}`: {error}",
                    event.as_str()
                ),
            },
        }
    }

    #[allow(clippy::unused_self)]
    fn run_command_with_timeout(
        &self,
        mut child: CommandWithStdin,
        command: &str,
        event: HookEvent,
        tool_name: &str,
        payload: &str,
        timeout_ms: u64,
    ) -> HookCommandOutcome {
        let payload = payload.to_owned();
        let (tx, rx) = mpsc::channel();

        std::thread::spawn(move || {
            let result = child.output_with_stdin(payload.as_bytes());
            let _ = tx.send(result);
        });

        match rx.recv_timeout(Duration::from_millis(timeout_ms)) {
            Ok(Ok(output)) => interpret_output(command, event, tool_name, &output),
            Ok(Err(error)) => HookCommandOutcome::Warn {
                message: format!(
                    "{} hook `{command}` failed to start for `{tool_name}`: {error}",
                    event.as_str()
                ),
            },
            Err(_) => HookCommandOutcome::Warn {
                message: format!(
                    "{} hook `{command}` timed out after {timeout_ms}ms for `{tool_name}`",
                    event.as_str()
                ),
            },
        }
    }
}

fn should_run_for_tool(entry: &HookEntry, tool_name: &str) -> bool {
    if let Some(only) = &entry.only_tool_names {
        if !only.iter().any(|name| name == tool_name) {
            return false;
        }
    }
    if let Some(exclude) = &entry.exclude_tool_names {
        if exclude.iter().any(|name| name == tool_name) {
            return false;
        }
    }
    true
}

fn interpret_output(
    command: &str,
    event: HookEvent,
    tool_name: &str,
    output: &std::process::Output,
) -> HookCommandOutcome {
    let stdout = String::from_utf8_lossy(&output.stdout).trim().to_string();
    let stderr = String::from_utf8_lossy(&output.stderr).trim().to_string();
    let message = (!stdout.is_empty()).then_some(stdout);
    match output.status.code() {
        Some(0) => HookCommandOutcome::Allow { message },
        Some(2) => HookCommandOutcome::Deny { message },
        Some(code) => HookCommandOutcome::Warn {
            message: format_hook_warning(command, code, message.as_deref(), stderr.as_str()),
        },
        None => HookCommandOutcome::Warn {
            message: format!(
                "{} hook `{command}` terminated by signal while handling `{tool_name}`",
                event.as_str()
            ),
        },
    }
}

enum HookCommandOutcome {
    Allow { message: Option<String> },
    Deny { message: Option<String> },
    Warn { message: String },
}

fn parse_tool_input(tool_input: &str) -> serde_json::Value {
    serde_json::from_str(tool_input).unwrap_or_else(|_| json!({ "raw": tool_input }))
}

fn format_hook_warning(command: &str, code: i32, stdout: Option<&str>, stderr: &str) -> String {
    let mut message =
        format!("Hook `{command}` exited with status {code}; allowing tool execution to continue");
    if let Some(stdout) = stdout.filter(|stdout| !stdout.is_empty()) {
        message.push_str(": ");
        message.push_str(stdout);
    } else if !stderr.is_empty() {
        message.push_str(": ");
        message.push_str(stderr);
    }
    message
}

fn shell_command(command: &str) -> CommandWithStdin {
    #[cfg(windows)]
    let mut command_builder = {
        let mut command_builder = Command::new("cmd");
        command_builder.arg("/C").arg(command);
        CommandWithStdin::new(command_builder)
    };

    #[cfg(not(windows))]
    let command_builder = {
        let mut command_builder = Command::new("sh");
        command_builder.arg("-lc").arg(command);
        CommandWithStdin::new(command_builder)
    };

    command_builder
}

struct CommandWithStdin {
    command: Command,
}

impl CommandWithStdin {
    fn new(command: Command) -> Self {
        Self { command }
    }

    fn stdin(&mut self, cfg: std::process::Stdio) -> &mut Self {
        self.command.stdin(cfg);
        self
    }

    fn stdout(&mut self, cfg: std::process::Stdio) -> &mut Self {
        self.command.stdout(cfg);
        self
    }

    fn stderr(&mut self, cfg: std::process::Stdio) -> &mut Self {
        self.command.stderr(cfg);
        self
    }

    fn env<K, V>(&mut self, key: K, value: V) -> &mut Self
    where
        K: AsRef<OsStr>,
        V: AsRef<OsStr>,
    {
        self.command.env(key, value);
        self
    }

    fn output_with_stdin(&mut self, stdin: &[u8]) -> std::io::Result<std::process::Output> {
        let mut child = self.command.spawn()?;
        if let Some(mut child_stdin) = child.stdin.take() {
            use std::io::Write;
            child_stdin.write_all(stdin)?;
        }
        child.wait_with_output()
    }
}

#[cfg(test)]
mod tests {
    use super::{HookRunResult, HookRunner};
    use crate::config::{HookEntry, HookEntryConfig, RuntimeFeatureConfig, RuntimeHookConfig};

    #[test]
    fn allows_exit_code_zero_and_captures_stdout() {
        let runner = HookRunner::new(RuntimeHookConfig::new(
            vec![shell_snippet("printf 'pre ok'")],
            Vec::new(),
        ));

        let result = runner.run_pre_tool_use("Read", r#"{"path":"README.md"}"#);

        assert_eq!(result, HookRunResult::allow(vec!["pre ok".to_string()]));
    }

    #[test]
    fn denies_exit_code_two() {
        let runner = HookRunner::new(RuntimeHookConfig::new(
            vec![shell_snippet("printf 'blocked by hook'; exit 2")],
            Vec::new(),
        ));

        let result = runner.run_pre_tool_use("Bash", r#"{"command":"pwd"}"#);

        assert!(result.is_denied());
        assert_eq!(result.messages(), &["blocked by hook".to_string()]);
    }

    #[test]
    fn warns_for_other_non_zero_statuses() {
        let runner = HookRunner::from_feature_config(&RuntimeFeatureConfig::default().with_hooks(
            RuntimeHookConfig::new(
                vec![shell_snippet("printf 'warning hook'; exit 1")],
                Vec::new(),
            ),
        ));

        let result = runner.run_pre_tool_use("Edit", r#"{"file":"src/lib.rs"}"#);

        assert!(!result.is_denied());
        assert!(result
            .messages()
            .iter()
            .any(|message| message.contains("allowing tool execution to continue")));
    }

    #[test]
    fn only_tool_names_skips_non_matching_tool() {
        let runner = HookRunner::new(RuntimeHookConfig::new_rich(
            vec![HookEntryConfig::Rich(HookEntry {
                command: shell_snippet("printf 'should not run'; exit 2"),
                only_tool_names: Some(vec!["bash".to_string()]),
                ..HookEntry::default()
            })],
            Vec::new(),
            Vec::new(),
        ));

        let result = runner.run_pre_tool_use("read_file", r"{}");

        assert!(!result.is_denied());
        assert!(result.messages().is_empty());
    }

    #[test]
    fn only_tool_names_runs_for_matching_tool() {
        let runner = HookRunner::new(RuntimeHookConfig::new_rich(
            vec![HookEntryConfig::Rich(HookEntry {
                command: shell_snippet("printf 'matched'"),
                only_tool_names: Some(vec!["bash".to_string()]),
                ..HookEntry::default()
            })],
            Vec::new(),
            Vec::new(),
        ));

        let result = runner.run_pre_tool_use("bash", r"{}");

        assert!(!result.is_denied());
        assert_eq!(result.messages(), &["matched".to_string()]);
    }

    #[test]
    fn exclude_tool_names_skips_matching_tool() {
        let runner = HookRunner::new(RuntimeHookConfig::new_rich(
            vec![HookEntryConfig::Rich(HookEntry {
                command: shell_snippet("printf 'should not run'; exit 2"),
                exclude_tool_names: Some(vec!["bash".to_string()]),
                ..HookEntry::default()
            })],
            Vec::new(),
            Vec::new(),
        ));

        let result = runner.run_pre_tool_use("bash", r"{}");

        assert!(!result.is_denied());
        assert!(result.messages().is_empty());
    }

    #[test]
    fn exclude_tool_names_runs_for_non_matching_tool() {
        let runner = HookRunner::new(RuntimeHookConfig::new_rich(
            vec![HookEntryConfig::Rich(HookEntry {
                command: shell_snippet("printf 'ran'"),
                exclude_tool_names: Some(vec!["bash".to_string()]),
                ..HookEntry::default()
            })],
            Vec::new(),
            Vec::new(),
        ));

        let result = runner.run_pre_tool_use("read_file", r"{}");

        assert!(!result.is_denied());
        assert_eq!(result.messages(), &["ran".to_string()]);
    }

    #[test]
    fn timeout_produces_warning_on_slow_command() {
        let runner = HookRunner::new(RuntimeHookConfig::new_rich(
            vec![HookEntryConfig::Rich(HookEntry {
                command: shell_snippet("sleep 10"),
                timeout_ms: Some(100),
                ..HookEntry::default()
            })],
            Vec::new(),
            Vec::new(),
        ));

        let result = runner.run_pre_tool_use("bash", r"{}");

        assert!(!result.is_denied());
        assert!(
            result.messages().iter().any(|m| m.contains("timed out")),
            "expected timeout warning, got: {:?}",
            result.messages()
        );
    }

    #[test]
    fn plain_string_config_backward_compat() {
        let runner = HookRunner::new(RuntimeHookConfig::new(
            vec![shell_snippet("printf 'compat ok'")],
            Vec::new(),
        ));

        let result = runner.run_pre_tool_use("Read", r"{}");

        assert!(!result.is_denied());
        assert_eq!(result.messages(), &["compat ok".to_string()]);
    }

    #[test]
    fn notification_hook_fires() {
        let runner = HookRunner::new(RuntimeHookConfig::new_rich(
            Vec::new(),
            Vec::new(),
            vec![HookEntryConfig::Simple(shell_snippet("printf 'notified'"))],
        ));

        let result = runner.run_notification();

        assert!(!result.is_denied());
        assert_eq!(result.messages(), &["notified".to_string()]);
    }

    #[cfg(windows)]
    fn shell_snippet(script: &str) -> String {
        script.replace('\'', "\"")
    }

    #[cfg(not(windows))]
    fn shell_snippet(script: &str) -> String {
        script.to_string()
    }
}
