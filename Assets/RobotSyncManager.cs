//using System;
//using System.Text;
//using System.Net.Sockets;
//using System.Threading;
//using System.Threading.Tasks;
//using System.Collections.Concurrent;
//using UnityEngine;
//using UnityEngine.InputSystem;
//using UnityEngine.InputSystem.Controls;
//using UnityEngine.Events;

//public class RobotSyncManager : MonoBehaviour
//{
//    [Header("TCP 目标")]
//    public string robotIP = "192.168.149.1";
//    public int robotPort = 5075;

//    [Header("发送频率与输入")]
//    public float sendHz = 30f;
//    [Range(0f, 0.5f)] public float deadzone = 0.15f;

//    [Header("XR 输入（推荐把这两个指到 Locomotion 的 Action）")]
//    public InputActionReference moveAction;  // 通常为 Vector2 (x:left/right, y:forward/back)
//    public InputActionReference turnAction;  // 可能是 Vector2 或 float

//    [Header("事件（外部订阅）")]
//    public UnityEvent<string> onColorSignalReceived; // 会传 "RED" 或 "GREEN"

//    // 内部
//    private float _interval;
//    private TcpClient _client;
//    private NetworkStream _stream;
//    private CancellationTokenSource _cts;        // 当前连接的 token
//    private CancellationTokenSource _connectCts; // 控制 ConnectLoop 生命周期
//    private Task _sendTask, _recvTask;
//    private volatile bool _running;

//    private volatile float _lastV, _lastSteer;
//    private volatile bool _isGrabbing;

//    // 用于线程间安全地把机器人发来的颜色消息丢回主线程处理
//    private ConcurrentQueue<string> _recvQueue = new ConcurrentQueue<string>();

//    [Serializable]
//    struct ControlPayload
//    {
//        public float v;
//        public float steer;
//        public bool grab;
//        public string t;
//    }

//    void OnEnable()
//    {
//        moveAction?.action?.Enable();
//        turnAction?.action?.Enable();
//    }

//    void OnDisable()
//    {
//        try
//        {
//            moveAction?.action?.Disable();
//            turnAction?.action?.Disable();
//        }
//        catch { }
//    }

//    private void Start()
//    {
//        // 防止 sendHz 为 0 导致除 0
//        _interval = 1f / Mathf.Max(1f, sendHz);
//        _connectCts = new CancellationTokenSource();
//        // fire-and-forget connect loop (不阻塞 Start)
//        _ = ConnectLoop(_connectCts.Token);
//    }

//    private async Task ConnectLoop(CancellationToken connectToken)
//    {
//        while (!connectToken.IsCancellationRequested)
//        {
//            // 取消旧的连接任务（如果有）
//            try { _cts?.Cancel(); _cts?.Dispose(); } catch { }
//            _cts = CancellationTokenSource.CreateLinkedTokenSource(connectToken);
//            var token = _cts.Token;

//            try
//            {
//                _client = new TcpClient();
//                _client.NoDelay = true;
//                Debug.Log($"[TCP] Connecting {robotIP}:{robotPort} …");
//                await _client.ConnectAsync(robotIP, robotPort);
//                _stream = _client.GetStream();
//                Debug.Log("[TCP] Connected");
//                _running = true;

//                // 启动发送与接收（传入 token）
//                _sendTask = Task.Run(() => SendLoop(token));
//                _recvTask = Task.Run(() => RecvLoop(token));

//                // 等任一任务结束（异常或断开）
//                await Task.WhenAny(_sendTask, _recvTask);
//            }
//            catch (OperationCanceledException)
//            {
//                // 被取消，直接退出循环
//                break;
//            }
//            catch (Exception e)
//            {
//                Debug.LogWarning("[TCP] Connect error: " + e.Message);
//            }

//            _running = false;
//            try { _stream?.Close(); } catch { }
//            try { _client?.Close(); } catch { }

//            // 等待一段时间再重连（可被取消）
//            try { await Task.Delay(1000, connectToken); } catch (OperationCanceledException) { break; }
//        }
//    }

//    private async Task SendLoop(CancellationToken token)
//    {
//        var waitMs = (int)Mathf.Ceil(_interval * 1000f);
//        while (_running && _client?.Connected == true && _stream != null && !token.IsCancellationRequested)
//        {
//            try
//            {
//                // Build payload from the latest cached values (Update() 里写入)
//                var p = new ControlPayload
//                {
//                    v = Mathf.Clamp(_lastV, -1f, 1f),
//                    steer = Mathf.Clamp(_lastSteer, -1f, 1f),
//                    grab = _isGrabbing,
//                    t = DateTime.UtcNow.ToString("o")
//                };
//                var json = JsonUtility.ToJson(p) + "\n";
//                var bytes = Encoding.UTF8.GetBytes(json);
//                await _stream.WriteAsync(bytes, 0, bytes.Length, token);
//            }
//            catch (OperationCanceledException) { break; }
//            catch (Exception e)
//            {
//                Debug.LogWarning("[TCP] Send error: " + e.Message);
//                break;
//            }

//            try { await Task.Delay(waitMs, token); } catch (OperationCanceledException) { break; } catch { break; }
//        }
//    }

//    private async Task RecvLoop(CancellationToken token)
//    {
//        var buf = new byte[4096];
//        var sb = new StringBuilder();

//        while (_running && _client?.Connected == true && _stream != null && !token.IsCancellationRequested)
//        {
//            try
//            {
//                int n = await _stream.ReadAsync(buf, 0, buf.Length, token);
//                if (n <= 0) break;
//                var chunk = Encoding.UTF8.GetString(buf, 0, n);
//                sb.Append(chunk);

//                // 按换行分消息，保留最后一段（可能是半包）
//                var all = sb.ToString();
//                var parts = all.Split('\n');

//                for (int i = 0; i < parts.Length - 1; i++)
//                {
//                    var m = parts[i].Trim();
//                    if (string.IsNullOrEmpty(m)) continue;

//                    // 识别颜色消息：可能是 "COLOR_SIGNAL:RED" 或 直接 "RED"/"GREEN"
//                    if (m.StartsWith("COLOR_SIGNAL:", StringComparison.OrdinalIgnoreCase))
//                    {
//                        var col = m.Substring("COLOR_SIGNAL:".Length).Trim().ToUpperInvariant();
//                        if (col == "RED" || col == "GREEN")
//                        {
//                            _recvQueue.Enqueue(col);
//                        }
//                    }
//                    else
//                    {
//                        var col = m.ToUpperInvariant();
//                        if (col == "RED" || col == "GREEN")
//                        {
//                            _recvQueue.Enqueue(col);
//                        }
//                        else
//                        {
//                            // 非颜色信息：按你的要求忽略（不报错）
//                        }
//                    }
//                }

//                // 将最后不完整的部分保存下来
//                sb = new StringBuilder(parts.Length > 0 ? parts[parts.Length - 1] : "");
//            }
//            catch (OperationCanceledException) { break; }
//            catch (Exception e)
//            {
//                Debug.LogWarning("[TCP] Recv error: " + e.Message);
//                break;
//            }
//        }
//    }

//    void Update()
//    {
//        // 1) 先把接收到的颜色消息从队列投递到主线程事件（确保 UnityEvent 在主线程被触发）
//        while (_recvQueue.TryDequeue(out var color))
//        {
//            try { onColorSignalReceived?.Invoke(color); } catch (Exception e) { Debug.LogWarning("[RobotSync] onColor handler error: " + e.Message); }
//        }

//        // 2) 读取输入，写入 _lastV/_lastSteer（供 SendLoop 使用）
//        Vector2 move = Vector2.zero;
//        if (moveAction != null && moveAction.action != null && moveAction.action.enabled)
//        {
//            try { move = moveAction.action.ReadValue<Vector2>(); } catch { move = Vector2.zero; }
//        }
//        else
//        {
//            // 如果你希望可以加键盘调试，这里可以扩展（目前保持简洁）
//        }

//        // Turn 读取：兼容 Vector2 / StickControl / float
//        Vector2 rotate = Vector2.zero;
//        if (turnAction != null && turnAction.action != null && turnAction.action.enabled)
//        {
//            var act = turnAction.action;
//            bool gotVector2 = false;

//            if (act.expectedControlType == "Vector2")
//            {
//                rotate = act.ReadValue<Vector2>();
//                gotVector2 = true;
//            }
//            else
//            {
//                var ctrl = act.activeControl;
//                if (ctrl is StickControl || ctrl is Vector2Control)
//                {
//                    rotate = act.ReadValue<Vector2>();
//                    gotVector2 = true;
//                }
//                else if (act.controls.Count > 0 && (act.controls[0] is StickControl || act.controls[0] is Vector2Control))
//                {
//                    rotate = act.ReadValue<Vector2>();
//                    gotVector2 = true;
//                }
//            }

//            if (!gotVector2)
//            {
//                float tx = 0f;
//                try { tx = act.ReadValue<float>(); } catch { tx = 0f; }
//                rotate = new Vector2(tx, 0f);
//            }
//        }

//        // 兜底：Gamepad 右摇杆（若需要）
//        if (rotate == Vector2.zero)
//        {
//            var gp = Gamepad.current;
//            if (gp != null) rotate = gp.rightStick.ReadValue();
//        }

//        _lastV = Mathf.Abs(move.y) < deadzone ? 0f : move.y;
//        _lastSteer = Mathf.Abs(rotate.x) < deadzone ? 0f : rotate.x;
//    }

//    /// <summary>
//    /// 外部（例如 LightControl 的抓取回调）调用告知本地抓取状态变化。
//    /// isGrabbing: true 表示抓起，false 表示放下
//    /// grabbedObject: 通常传入被抓取物体 GameObject（可 null）
//    /// immediate: 若 true，会立刻向机器人发送一条抓取状态消息（文本），否则仅更新周期性 payload 的 grab 字段
//    /// </summary>
//    public void OnGrabStateChanged(bool isGrabbing, GameObject grabbedObject, bool immediate = true)
//    {
//        _isGrabbing = isGrabbing;
//        if (immediate)
//        {
//            _ = SendImmediateGrabState(isGrabbing, grabbedObject);
//        }
//    }

//    private async Task SendImmediateGrabState(bool isGrabbing, GameObject grabbedObject)
//    {
//        if (_client?.Connected != true || _stream == null) return;
//        try
//        {
//            string grabState = isGrabbing ? "GRAB_START" : "GRAB_END";
//            string name = grabbedObject != null ? grabbedObject.name : "unknown";
//            string message = $"{grabState}:{name}:{DateTime.UtcNow:o}\n";
//            var bytes = Encoding.UTF8.GetBytes(message);
//            await _stream.WriteAsync(bytes, 0, bytes.Length);
//        }
//        catch (Exception e)
//        {
//            Debug.LogWarning("[RobotSync] SendImmediateGrabState failed: " + e.Message);
//        }
//    }

//    private void OnDestroy()
//    {
//        try
//        {
//            _running = false;
//            _connectCts?.Cancel();
//            _cts?.Cancel();
//            _stream?.Close();
//            _client?.Close();
//        }
//        catch { }
//    }
//}

using System;
using System.Text;
using System.Net.Sockets;
using System.Threading;
using System.Threading.Tasks;
using System.Collections.Concurrent;
using System.Collections.Generic;
using UnityEngine;
using UnityEngine.InputSystem;
using UnityEngine.InputSystem.Controls;
using UnityEngine.Events;
using UnityEngine.XR;

public class RobotSyncManager : MonoBehaviour
{
    [Header("TCP 目标")]
    public string robotIP = "192.168.137.251";
    public int robotPort = 5075;

    [Header("发送频率与输入")]
    public float sendHz = 30f;
    [Range(0f, 0.5f)] public float deadzone = 0.15f;
    [Range(0f, 2f)] public float turnScale = 1f;

    [Header("XR 输入（把你项目里的动作拖进来）")]
    public InputActionReference moveAction;  // XRI LeftHand Locomotion / Move (Vector2)
    public InputActionReference turnAction;  // XRI RightHand Locomotion / Turn(Continuous)
    [Header("⭐ 按钮→一次性命令")]
    public InputActionReference rightGripAction;   // 例如 RightHand / PrimaryButton
    public InputActionReference rightTriggerAction;
    public InputActionReference leftTriggerAction;
    public string rightGripCommend = "right_grip";      // 要执行的动作名（默认 right_grip）
    public string rightTriggerCommend = "right_trigger";
    public string leftTriggerCommend = "left_trigger";
    public bool enableKeyboardButtonDebug = true; // J 键也触发 CMD:right_grip（调试用）

    [Header("起身动作安全阈值")]
    [Range(0.5f, 1f)] public float recoveryTriggerThreshold = 0.8f;
    [Min(0.1f)] public float recoveryHoldSeconds = 1.5f;
    [Min(0.1f)] public float recoveryNeutralSeconds = 0.5f;
    [Range(0f, 0.5f)] public float recoveryNeutralThreshold = 0.1f;

    [Header("事件（外部订阅）")]
    public UnityEvent<string> onColorSignalReceived = new UnityEvent<string>(); // "RED"/"GREEN" 颜色回传
    public UnityEvent<string> onBallDetected = new UnityEvent<string>(); // 小球检测回传
    public UnityEvent onActionTriggered = new UnityEvent();                     // 新增：动作触发

    // 内部
    private float _interval;
    private TcpClient _client;
    private NetworkStream _stream;
    private CancellationTokenSource _cts;
    private CancellationTokenSource _connectCts;
    private Task _sendTask, _recvTask;
    private volatile bool _running;
    private readonly SemaphoreSlim _writeLock = new SemaphoreSlim(1, 1);

    private volatile float _lastV, _lastSteer;
    private volatile bool _isGrabbing;

    private readonly ConcurrentQueue<string> _recvQueue = new ConcurrentQueue<string>();
    private readonly ConcurrentQueue<string> _ballQueue = new ConcurrentQueue<string>();

    // ⭐ 新增：按钮长按只触发一次的“上升沿”检测
    // private bool _buttonHeldPrev = false;
    private bool _rightGripHeldPrev = false;
    private float _neutralInputSince = -1f;
    private float _rightTriggerHoldSince = -1f;
    private float _leftTriggerHoldSince = -1f;
    private bool _rightRecoverySent = false;
    private bool _leftRecoverySent = false;
    [Serializable]
    struct ControlPayload
    {
        public float v;
        public float steer;
        public bool grab;
        public string t;
    }

    void OnEnable()
    {
        moveAction?.action?.Enable();
        turnAction?.action?.Enable();
        //buttonAction?.action?.Enable();   // ⭐ 启用按钮
        rightGripAction?.action?.Enable();
        rightTriggerAction?.action?.Enable();
        leftTriggerAction?.action?.Enable();
    }

    void OnDisable()
    {
        try
        {
            moveAction?.action?.Disable();
            turnAction?.action?.Disable();
            //buttonAction?.action?.Disable();
            rightGripAction?.action?.Disable();
            rightTriggerAction?.action?.Disable();
            leftTriggerAction?.action?.Disable();
        }
        catch { }
    }

    private void Start()
    {
        _interval = 1f / Mathf.Max(1f, sendHz);
        _connectCts = new CancellationTokenSource();
        _ = ConnectLoop(_connectCts.Token);
    }

    private async Task ConnectLoop(CancellationToken connectToken)
    {
        while (!connectToken.IsCancellationRequested)
        {
            try { _cts?.Cancel(); _cts?.Dispose(); } catch { }
            _cts = CancellationTokenSource.CreateLinkedTokenSource(connectToken);
            var token = _cts.Token;

            try
            {
                _client = new TcpClient();
                _client.NoDelay = true;
                Debug.Log($"[TCP] Connecting {robotIP}:{robotPort} …");
                await _client.ConnectAsync(robotIP, robotPort);
                _stream = _client.GetStream();
                Debug.Log("[TCP] Connected");
                _running = true;

                _sendTask = Task.Run(() => SendLoop(token));
                _recvTask = Task.Run(() => RecvLoop(token));

                await Task.WhenAny(_sendTask, _recvTask);
            }
            catch (OperationCanceledException) { break; }
            catch (Exception e)
            {
                Debug.LogWarning("[TCP] Connect error: " + e.Message);
            }

            _running = false;
            try { _stream?.Close(); } catch { }
            try { _client?.Close(); } catch { }
            try { await Task.Delay(1000, connectToken); } catch (OperationCanceledException) { break; }
        }
    }

    private async Task SendLoop(CancellationToken token)
    {
        var waitMs = (int)Mathf.Ceil(_interval * 1000f);
        while (_running && _client?.Connected == true && _stream != null && !token.IsCancellationRequested)
        {
            try
            {
                var p = new ControlPayload
                {
                    v = Mathf.Clamp(_lastV, -1f, 1f),
                    steer = Mathf.Clamp(_lastSteer, -1f, 1f),
                    grab = _isGrabbing,
                    t = DateTime.UtcNow.ToString("o")
                };
                var json = JsonUtility.ToJson(p) + "\n";
                var bytes = Encoding.UTF8.GetBytes(json);
                await WriteBytes(bytes, token);
            }
            catch (OperationCanceledException) { break; }
            catch (Exception e)
            {
                Debug.LogWarning("[TCP] Send error: " + e.Message);
                break;
            }

            try { await Task.Delay(waitMs, token); } catch (OperationCanceledException) { break; } catch { break; }
        }
    }

    private async Task RecvLoop(CancellationToken token)
    {
        var buf = new byte[4096];
        var sb = new StringBuilder();

        while (_running && _client?.Connected == true && _stream != null && !token.IsCancellationRequested)
        {
            try
            {
                int n = await _stream.ReadAsync(buf, 0, buf.Length, token);
                if (n <= 0) break;
                var chunk = Encoding.UTF8.GetString(buf, 0, n);
                sb.Append(chunk);

                var all = sb.ToString();
                var parts = all.Split('\n');

                for (int i = 0; i < parts.Length - 1; i++)
                {
                    var m = parts[i].Trim();
                    if (string.IsNullOrEmpty(m)) continue;

                    if (m.Equals("BALL_DETECTED", StringComparison.OrdinalIgnoreCase))
                    {
                        _ballQueue.Enqueue(string.Empty);
                        Debug.Log("[TCP] Recv : BALL_DETECTED");
                        continue;
                    }

                    if (m.StartsWith("BALL_DETECTED:", StringComparison.OrdinalIgnoreCase))
                    {
                        var targetId = m.Substring("BALL_DETECTED:".Length).Trim();
                        _ballQueue.Enqueue(targetId);
                        Debug.Log("[TCP] Recv : BALL_DETECTED -> " + (string.IsNullOrEmpty(targetId) ? "<default>" : targetId));
                        continue;
                    }

                    if (m.StartsWith("COLOR_SIGNAL:", StringComparison.OrdinalIgnoreCase))
                    {
                        var col = m.Substring("COLOR_SIGNAL:".Length).Trim().ToUpperInvariant();
                        //if (col == "RED" || col == "GREEN") _recvQueue.Enqueue(col);
                        if (col == "RED" || col == "GREEN")
                        {
                            _recvQueue.Enqueue(col);
                            Debug.Log($"[TCP] Recv : {col}"); // 添加日志输出
                        }
                    }
                    else
                    {
                        var col = m.ToUpperInvariant();
                        //if (col == "RED" || col == "GREEN") _recvQueue.Enqueue(col);
                        if (col == "RED" || col == "GREEN")
                        {
                            _recvQueue.Enqueue(col);
                            Debug.Log($"[TCP] Recv : {col}"); // 添加日志输出
                        }
                    }
                }

                sb = new StringBuilder(parts.Length > 0 ? parts[parts.Length - 1] : "");
            }
            catch (OperationCanceledException) { break; }
            catch (Exception e)
            {
                Debug.LogWarning("[TCP] Recv error: " + e.Message);
                break;
            }
        }
    }

    void Update()
    {
        // 键盘调试：R 键模拟树莓派发来红色火焰信号
        if (enableKeyboardButtonDebug && Keyboard.current != null && Keyboard.current.rKey.wasPressedThisFrame)
        {
            _recvQueue.Enqueue("RED");
            Debug.Log("[TCP] Recv : RED (keyboard debug)");
        }

        while (_ballQueue.TryDequeue(out var targetId))
        {
            try { onBallDetected?.Invoke(targetId); }
            catch (Exception e) { Debug.LogWarning("[RobotSync] onBallDetected handler error: " + e.Message); }
        }

        // 派发颜色消息
        while (_recvQueue.TryDequeue(out var color))
        {
            try { onColorSignalReceived?.Invoke(color); }
            catch (Exception e) { Debug.LogWarning("[RobotSync] onColor handler error: " + e.Message); }
        }

        // Move
        Vector2 move = Vector2.zero;
        if (moveAction != null && moveAction.action != null && moveAction.action.enabled)
        {
            try { move = moveAction.action.ReadValue<Vector2>(); } catch { move = Vector2.zero; }
        }

        // Turn（兼容 Vector2/float）
        Vector2 rotate = Vector2.zero;
        if (turnAction != null && turnAction.action != null && turnAction.action.enabled)
        {
            var act = turnAction.action;
            bool gotVector2 = false;

            if (act.expectedControlType == "Vector2")
            {
                rotate = act.ReadValue<Vector2>();
                gotVector2 = true;
            }
            else
            {
                var ctrl = act.activeControl;
                if (ctrl is StickControl || ctrl is Vector2Control)
                {
                    rotate = act.ReadValue<Vector2>();
                    gotVector2 = true;
                }
                else if (act.controls.Count > 0 && (act.controls[0] is StickControl || act.controls[0] is Vector2Control))
                {
                    rotate = act.ReadValue<Vector2>();
                    gotVector2 = true;
                }
            }

            if (!gotVector2)
            {
                float tx = 0f;
                try { tx = act.ReadValue<float>(); } catch { tx = 0f; }
                rotate = new Vector2(tx, 0f);
            }
        }

        // Gamepad 兜底
        if (rotate == Vector2.zero)
        {
            var gp = Gamepad.current;
            if (gp != null) rotate = gp.rightStick.ReadValue();
        }

        float manualV = Mathf.Abs(move.y) < deadzone ? 0f : move.y;
        float manualSteer = Mathf.Abs(rotate.x) < deadzone ? 0f : Mathf.Clamp(rotate.x * turnScale, -1f, 1f);
        _lastV = manualV;
        _lastSteer = manualSteer;

        bool recoveryInputNeutral = Mathf.Abs(_lastV) <= recoveryNeutralThreshold
            && Mathf.Abs(_lastSteer) <= recoveryNeutralThreshold;
        if (recoveryInputNeutral)
        {
            if (_neutralInputSince < 0f) _neutralInputSince = Time.unscaledTime;
        }
        else
        {
            _neutralInputSince = -1f;
            _rightTriggerHoldSince = -1f;
            _leftTriggerHoldSince = -1f;
        }
    
        // if (Time.frameCount % 30 == 0)
        // {
        //     Debug.Log($"[INPUT] move={_lastV:F2}, steer={_lastSteer:F2}, rawTurn={rotate.x:F2}");
        // }

        //// ⭐ 按钮：长按只触发一次（上升沿）
        //if (_client?.Connected == true && _stream != null)
        //{
        //    bool heldNow = false;

        //    if (buttonAction != null && buttonAction.action != null && buttonAction.action.enabled)
        //    {
        //        var act = buttonAction.action;

        //        // 优先用 ButtonControl 的 isPressed；否则读 float > 0.5
        //        if (act.activeControl is ButtonControl btnCtrl)
        //            heldNow = btnCtrl.isPressed;
        //        else
        //        {
        //            try { heldNow = act.ReadValue<float>() > 0.5f; } catch { heldNow = false; }
        //        }
        //    }

        //    // 键盘调试：J 键按住同样算 held
        //    if (enableKeyboardButtonDebug && Keyboard.current != null && Keyboard.current.jKey.isPressed)
        //        heldNow = true;

        //    // 上升沿：本帧才变为按下 → 发一次 CMD
        //    if (heldNow && !_buttonHeldPrev)
        //    {
        //        _ = SendImmediateCmd(buttonCommand);
        //    }

        //    // 更新上一帧状态
        //    _buttonHeldPrev = heldNow;
        //}
        HandleButtonEdge(rightGripAction, ref _rightGripHeldPrev, rightGripCommend, true);
        HandleRecoveryHold(rightTriggerAction, ref _rightTriggerHoldSince, ref _rightRecoverySent, rightTriggerCommend);
        HandleRecoveryHold(leftTriggerAction, ref _leftTriggerHoldSince, ref _leftRecoverySent, leftTriggerCommend);

        // 键盘调试：J键=灭火
        if (enableKeyboardButtonDebug && Keyboard.current != null && Keyboard.current.jKey.wasPressedThisFrame)
        {
            onActionTriggered?.Invoke();
            _ = SendImmediateCmd(rightGripCommend);
        }
    }

    private void HandleButtonEdge(InputActionReference actionRef, ref bool heldPrev, string cmd, bool triggerLocalAction)
    {
        bool heldNow = ReadHeld(actionRef);

        if (heldNow && !heldPrev)
        {
            if (triggerLocalAction)
            {
                onActionTriggered?.Invoke();
            }
            _ = SendImmediateCmd(cmd);
        }

        heldPrev = heldNow;
    }

    private bool ReadHeld(InputActionReference actionRef)
    {
        if (actionRef == null || actionRef.action == null || !actionRef.action.enabled)
            return false;

        var act = actionRef.action;

        if (act.activeControl is ButtonControl btnCtrl)
            return btnCtrl.isPressed;

        try
        {
            return act.ReadValue<float>() > 0.5f;
        }
        catch
        {
            return false;
        }
    }

    private async Task SendImmediateCmd(string cmd)
    {
        if (string.IsNullOrEmpty(cmd)) return;
        if (_client?.Connected != true || _stream == null) return;

        try
        {
            string line = $"CMD:{cmd}\n";
            var bytes = Encoding.UTF8.GetBytes(line);
            await WriteBytes(bytes, CancellationToken.None);
            Debug.Log("[TCP] CMD sent: " + line.Trim());
        }
        catch (Exception e)
        {
            Debug.LogWarning("[TCP] CMD send error: " + e.Message);
        }
    }

    private void HandleRecoveryHold(
        InputActionReference actionRef,
        ref float holdSince,
        ref bool commandSent,
        string cmd)
    {
        float value = ReadAnalog(actionRef);
        if (value < recoveryTriggerThreshold)
        {
            ResetRecoveryHold(ref holdSince, ref commandSent);
            return;
        }

        bool neutralReady = _neutralInputSince >= 0f
            && Time.unscaledTime - _neutralInputSince >= recoveryNeutralSeconds;
        if (!neutralReady)
        {
            holdSince = -1f;
            return;
        }

        if (holdSince < 0f) holdSince = Time.unscaledTime;
        if (!commandSent && Time.unscaledTime - holdSince >= recoveryHoldSeconds)
        {
            commandSent = true;
            _ = SendImmediateCmd(cmd);
            Debug.Log($"[SAFE INPUT] Recovery command sent after hold: {cmd}");
        }
    }

    private static void ResetRecoveryHold(ref float holdSince, ref bool commandSent)
    {
        holdSince = -1f;
        commandSent = false;
    }

    private float ReadAnalog(InputActionReference actionRef)
    {
        if (actionRef == null || actionRef.action == null || !actionRef.action.enabled)
            return 0f;

        try { return Mathf.Clamp01(actionRef.action.ReadValue<float>()); }
        catch
        {
            if (actionRef.action.activeControl is ButtonControl buttonControl)
                return buttonControl.isPressed ? 1f : 0f;
            return 0f;
        }
    }

    // 抓取状态（保留）
    public void OnGrabStateChanged(bool isGrabbing, GameObject grabbedObject, bool immediate = true)
    {
        _isGrabbing = isGrabbing;
        if (immediate) _ = SendImmediateGrabState(isGrabbing, grabbedObject);
    }

    private async Task SendImmediateGrabState(bool isGrabbing, GameObject grabbedObject)
    {
        if (_client?.Connected != true || _stream == null) return;
        try
        {
            string grabState = isGrabbing ? "GRAB_START" : "GRAB_END";
            string name = grabbedObject != null ? grabbedObject.name : "unknown";
            string message = $"{grabState}:{name}:{DateTime.UtcNow:o}\n";
            var bytes = Encoding.UTF8.GetBytes(message);
            await WriteBytes(bytes, CancellationToken.None);
        }
        catch (Exception e)
        {
            Debug.LogWarning("[RobotSync] SendImmediateGrabState failed: " + e.Message);
        }
    }

    private async Task WriteBytes(byte[] bytes, CancellationToken token)
    {
        await _writeLock.WaitAsync(token);
        try
        {
            if (_client?.Connected == true && _stream != null)
                await _stream.WriteAsync(bytes, 0, bytes.Length, token);
        }
        finally
        {
            _writeLock.Release();
        }
    }

    private void OnDestroy()
    {
        try
        {
            _running = false;
            _connectCts?.Cancel();
            _cts?.Cancel();
            _stream?.Close();
            _client?.Close();
        }
        catch { }
    }
}
