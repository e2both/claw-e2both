use std::collections::HashMap;
use std::sync::{Arc, Mutex};

use runtime::{
    ApiClient, ApiRequest, AssistantEvent, ConversationRuntime, RuntimeError, Session,
    StaticToolExecutor, TokenUsage, ToolError, ToolExecutor,
};
use runtime::{
    PermissionMode, PermissionPolicy, PermissionPromptDecision, PermissionPrompter,
    PermissionRequest,
};

// ---------------------------------------------------------------------------
// ScriptedApiClient: returns canned responses in order
// ---------------------------------------------------------------------------

struct ScriptedApiClient {
    responses: Vec<Vec<AssistantEvent>>,
    call_index: std::sync::atomic::AtomicUsize,
}

impl ScriptedApiClient {
    fn new(responses: Vec<Vec<AssistantEvent>>) -> Self {
        Self {
            responses,
            call_index: std::sync::atomic::AtomicUsize::new(0),
        }
    }
}

impl ApiClient for ScriptedApiClient {
    fn stream(&mut self, _request: ApiRequest) -> Result<Vec<AssistantEvent>, RuntimeError> {
        let idx = self
            .call_index
            .fetch_add(1, std::sync::atomic::Ordering::SeqCst);
        self.responses
            .get(idx)
            .cloned()
            .ok_or_else(|| RuntimeError::new("no more scripted responses"))
    }
}

// ---------------------------------------------------------------------------
// RecordingToolExecutor: records calls and returns canned results
// ---------------------------------------------------------------------------

struct RecordingToolExecutor {
    results: HashMap<String, String>,
    calls: Arc<Mutex<Vec<(String, String)>>>,
}

impl RecordingToolExecutor {
    fn new(results: HashMap<String, String>) -> Self {
        Self {
            results,
            calls: Arc::new(Mutex::new(Vec::new())),
        }
    }
}

impl ToolExecutor for RecordingToolExecutor {
    fn execute(&mut self, tool_name: &str, input: &str) -> Result<String, ToolError> {
        self.calls
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .push((tool_name.to_string(), input.to_string()));
        self.results
            .get(tool_name)
            .cloned()
            .ok_or_else(|| ToolError::new(format!("unknown tool: {tool_name}")))
    }
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

fn simple_text_response(text: &str) -> Vec<AssistantEvent> {
    vec![
        AssistantEvent::TextDelta(text.to_string()),
        AssistantEvent::MessageStop,
    ]
}

fn tool_call_response(tool_id: &str, tool_name: &str, input: &str) -> Vec<AssistantEvent> {
    vec![
        AssistantEvent::ToolUse {
            id: tool_id.to_string(),
            name: tool_name.to_string(),
            input: input.to_string(),
        },
        AssistantEvent::MessageStop,
    ]
}

fn tool_call_with_text_response(
    text: &str,
    tool_id: &str,
    tool_name: &str,
    input: &str,
) -> Vec<AssistantEvent> {
    vec![
        AssistantEvent::TextDelta(text.to_string()),
        AssistantEvent::ToolUse {
            id: tool_id.to_string(),
            name: tool_name.to_string(),
            input: input.to_string(),
        },
        AssistantEvent::MessageStop,
    ]
}

fn default_policy() -> PermissionPolicy {
    PermissionPolicy::new(PermissionMode::DangerFullAccess)
}

fn default_prompt() -> Vec<String> {
    vec!["test system prompt".to_string()]
}

// ---------------------------------------------------------------------------
// Test 1: Simple text response
// ---------------------------------------------------------------------------

#[test]
fn simple_text_response_single_turn() {
    let api = ScriptedApiClient::new(vec![simple_text_response("Hello, world!")]);
    let executor = StaticToolExecutor::new();
    let mut runtime = ConversationRuntime::new(
        Session::new(),
        api,
        executor,
        default_policy(),
        default_prompt(),
    );

    let summary = runtime.run_turn("hi", None).expect("turn should succeed");

    assert_eq!(
        summary.iterations, 1,
        "no tool calls means single iteration"
    );
    assert_eq!(summary.assistant_messages.len(), 1);
    assert!(summary.tool_results.is_empty());
    assert_eq!(runtime.session().messages.len(), 2); // user + assistant
}

// ---------------------------------------------------------------------------
// Test 2: Single tool call cycle
// ---------------------------------------------------------------------------

#[test]
fn single_tool_call_cycle() {
    let api = ScriptedApiClient::new(vec![
        tool_call_with_text_response("Let me check.", "t1", "lookup", "query"),
        simple_text_response("The answer is 42."),
    ]);

    let mut results = HashMap::new();
    results.insert("lookup".to_string(), "42".to_string());
    let executor = RecordingToolExecutor::new(results);
    let calls = Arc::clone(&executor.calls);

    let mut runtime = ConversationRuntime::new(
        Session::new(),
        api,
        executor,
        default_policy(),
        default_prompt(),
    );

    let summary = runtime
        .run_turn("what is the answer?", None)
        .expect("turn should succeed");

    assert_eq!(summary.iterations, 2);
    assert_eq!(summary.assistant_messages.len(), 2);
    assert_eq!(summary.tool_results.len(), 1);

    let recorded = calls
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner);
    assert_eq!(recorded.len(), 1);
    assert_eq!(recorded[0].0, "lookup");
    assert_eq!(recorded[0].1, "query");
}

// ---------------------------------------------------------------------------
// Test 3: Multi-tool chain
// ---------------------------------------------------------------------------

#[test]
fn multi_tool_chain() {
    let api = ScriptedApiClient::new(vec![
        tool_call_response("t1", "tool_a", "input_a"),
        tool_call_response("t2", "tool_b", "input_b"),
        simple_text_response("All done."),
    ]);

    let mut results = HashMap::new();
    results.insert("tool_a".to_string(), "result_a".to_string());
    results.insert("tool_b".to_string(), "result_b".to_string());
    let executor = RecordingToolExecutor::new(results);
    let calls = Arc::clone(&executor.calls);

    let mut runtime = ConversationRuntime::new(
        Session::new(),
        api,
        executor,
        default_policy(),
        default_prompt(),
    );

    let summary = runtime
        .run_turn("chain tools", None)
        .expect("turn should succeed");

    assert_eq!(summary.iterations, 3);
    assert_eq!(summary.assistant_messages.len(), 3);
    assert_eq!(summary.tool_results.len(), 2);

    let recorded = calls
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner);
    assert_eq!(recorded.len(), 2);
    assert_eq!(recorded[0].0, "tool_a");
    assert_eq!(recorded[1].0, "tool_b");
}

// ---------------------------------------------------------------------------
// Test 4: Permission denial
// ---------------------------------------------------------------------------

#[test]
fn permission_denial_produces_error_result() {
    struct DenyPrompter;
    impl PermissionPrompter for DenyPrompter {
        fn decide(&mut self, _request: &PermissionRequest) -> PermissionPromptDecision {
            PermissionPromptDecision::Deny {
                reason: "denied by test".to_string(),
            }
        }
    }

    let api = ScriptedApiClient::new(vec![
        tool_call_response("t1", "dangerous", "payload"),
        simple_text_response("Okay, I could not run it."),
    ]);

    let executor = StaticToolExecutor::new().register("dangerous", |_| {
        panic!("tool should not execute when permission is denied");
    });

    let mut runtime = ConversationRuntime::new(
        Session::new(),
        api,
        executor,
        PermissionPolicy::new(PermissionMode::WorkspaceWrite),
        default_prompt(),
    );

    let summary = runtime
        .run_turn("do something dangerous", Some(&mut DenyPrompter))
        .expect("turn should succeed after denial");

    assert_eq!(summary.tool_results.len(), 1);
    let block = &summary.tool_results[0].blocks[0];
    match block {
        runtime::ContentBlock::ToolResult {
            is_error, output, ..
        } => {
            assert!(is_error);
            assert!(
                output.contains("denied by test"),
                "unexpected denial output: {output}"
            );
        }
        other => panic!("expected ToolResult, got {other:?}"),
    }
}

// ---------------------------------------------------------------------------
// Test 5: Auto-compaction trigger
// ---------------------------------------------------------------------------

#[test]
fn auto_compaction_trigger_with_low_threshold() {
    let api = ScriptedApiClient::new(vec![vec![
        AssistantEvent::TextDelta("done".to_string()),
        AssistantEvent::Usage(TokenUsage {
            input_tokens: 500,
            output_tokens: 10,
            cache_creation_input_tokens: 0,
            cache_read_input_tokens: 0,
        }),
        AssistantEvent::MessageStop,
    ]]);

    // Pre-fill the session so compaction has messages to remove.
    let mut session = Session::new();
    session
        .messages
        .push(runtime::ConversationMessage::user_text("old message 1"));
    session
        .messages
        .push(runtime::ConversationMessage::assistant(vec![
            runtime::ContentBlock::Text {
                text: "old response 1".to_string(),
            },
        ]));
    session
        .messages
        .push(runtime::ConversationMessage::user_text("old message 2"));
    session
        .messages
        .push(runtime::ConversationMessage::assistant(vec![
            runtime::ContentBlock::Text {
                text: "old response 2".to_string(),
            },
        ]));

    let mut runtime = ConversationRuntime::new(
        session,
        api,
        StaticToolExecutor::new(),
        default_policy(),
        default_prompt(),
    )
    .with_auto_compaction_input_tokens_threshold(100);

    let summary = runtime
        .run_turn("trigger compaction", None)
        .expect("turn should succeed");

    assert!(
        summary.auto_compaction.is_some(),
        "auto compaction should fire when threshold exceeded"
    );
    let compaction = summary.auto_compaction.unwrap();
    assert!(
        compaction.removed_message_count > 0,
        "should have removed at least some messages"
    );
}

// ---------------------------------------------------------------------------
// Test 6: Max iterations limit
// ---------------------------------------------------------------------------

#[test]
fn max_iterations_limit_stops_loop() {
    // Return tool calls forever - the max_iterations should stop it.
    let api = ScriptedApiClient::new(vec![
        tool_call_response("t1", "loop_tool", "1"),
        tool_call_response("t2", "loop_tool", "2"),
        tool_call_response("t3", "loop_tool", "3"),
        tool_call_response("t4", "loop_tool", "4"),
    ]);

    let executor = StaticToolExecutor::new().register("loop_tool", |_| Ok("continue".to_string()));

    let mut runtime = ConversationRuntime::new(
        Session::new(),
        api,
        executor,
        default_policy(),
        default_prompt(),
    )
    .with_max_iterations(3);

    let err = runtime
        .run_turn("infinite loop", None)
        .expect_err("should fail due to max iterations");

    assert!(
        err.to_string().contains("maximum number of iterations"),
        "unexpected error message: {err}"
    );
}
