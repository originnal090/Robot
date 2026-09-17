using System;
using System.Collections.Generic;
using UnityEngine;

namespace HciRobot.Simulator
{
    [DisallowMultipleComponent]
    [RequireComponent(typeof(Rigidbody))]
    public sealed class TonyPiMotionDriver : MonoBehaviour
    {
        [SerializeField] private Rigidbody body;
        [SerializeField, Min(0f)] private float maximumForwardSpeed = 1.0f;
        // Placeholder until measured on hardware; independent of forward speed.
        [SerializeField, Min(0f)] private float maximumLateralSpeed = 0.2f;
        [Tooltip("Fallback continuous turn rate when measured full-stick calibration is disabled.")]
        [SerializeField, Min(0f)] private float maximumTurnDegreesPerSecond = 120f;
        [SerializeField, Range(0f, 1f)] private float deadzone = 0.20f;
        [SerializeField] private bool continuousMotion;

        [Header("Course compatibility")]
        [Tooltip("Automatically bridge right_grip/outfire actions to the course LightControl when present.")]
        [SerializeField] private bool enableCourseFireExtinguishAdapter = true;

        [Header("Scene scale")]
        [Tooltip("Unity units per real-world metre. The 21.5 default is calibrated for FactoryDay_HciRobot; rotation is unchanged.")]
        [SerializeField, Min(0f)] private float movementSpeedMultiplier = 21.5f;

        [Header("Measured TonyPi forward tiers")]
        [SerializeField] private bool useMeasuredForwardTiers = true;
        [SerializeField, Min(0f)] private float baselineForwardSpeed = 0.0375f;
        [SerializeField, Min(0f)] private float oneStepSpeedMultiplier = 0.40f;
        [SerializeField, Min(0f)] private float fastSpeedMultiplier = 2.0f;
        [SerializeField, Range(0f, 1f)] private float slowTierMaximum = 0.45f;
        [SerializeField, Range(0f, 1f)] private float fastTierMinimum = 0.75f;

        [Header("Measured TonyPi full-stick turn")]
        [Tooltip("Use the measured fast action angle and cadence for continuous full-stick turning.")]
        [SerializeField] private bool useMeasuredFullStickTurn = true;
        [Tooltip("Base yaw change produced by one full-stick fast turn action.")]
        [SerializeField, Min(0f)] private float fullStickTurnDegreesPerAction = 15f;
        [Tooltip("Duration of turn_left_fast/turn_right_fast in the course action files.")]
        [SerializeField, Min(0.01f)] private float fullStickTurnActionSeconds = 0.9f;
        [Tooltip("Service wait between repeated gait actions.")]
        [SerializeField, Min(0f)] private float turnRepeatIntervalSeconds = 0.3f;
        [Tooltip("Scales full-stick left-turn speed relative to the measured base angle.")]
        [SerializeField, Min(0f)] private float leftTurnMultiplier = 1f;
        [Tooltip("Scales full-stick right-turn speed relative to the measured base angle.")]
        [SerializeField, Min(0f)] private float rightTurnMultiplier = 0.5f;

        [Header("Persistent head pitch preview")]
        [SerializeField] private Transform headPitchTransform;
        [SerializeField] private float headDownPitchDegrees = 30f;
        [SerializeField] private float headCenterPitchDegrees = 10f;
        [SerializeField] private float headUpPitchDegrees = -10f;

        [Header("Single-step mirror preview (step sizes need hardware calibration)")]
        [SerializeField, Min(0f)] private float smallStepTurnDegrees = 8f;
        [SerializeField, Min(0f)] private float normalStepTurnDegrees = 20f;
        [SerializeField, Min(0f)] private float fastStepTurnDegrees = 30f;
        [SerializeField, Min(0f)] private float smallStepLateralMetres = 0.02f;
        [SerializeField, Min(0f)] private float normalStepLateralMetres = 0.05f;
        [SerializeField, Min(0f)] private float fastStepLateralMetres = 0.08f;
        [SerializeField, Min(0.1f)] private float smallLeftTurnSeconds = 1.35f;
        [SerializeField, Min(0.1f)] private float smallRightTurnSeconds = 1.45f;
        [SerializeField, Min(0.1f)] private float smallLeftLateralSeconds = 1f;
        [SerializeField, Min(0.1f)] private float smallRightLateralSeconds = 1.1f;
        [SerializeField, Min(0.1f)] private float normalStepSeconds = 1f;
        [SerializeField, Min(0.1f)] private float maximumMirrorWaitSeconds = 10f;

        private RobotCommand target = RobotCommand.Stop;
        private Vector2 actualOutput;
        private readonly HashSet<string> seenStepIds = new HashSet<string>();
        private string mirroredStepId;
        private float stepElapsed;
        private float stepDuration;
        private float stepDegrees;
        private float stepMetres;
        private float stepStartedRealtime;

        public event Action<RobotCommand> CommandApplied;
        public event Action<string> ActionReceived;
        public event Action<string, string> MirroredStepChanged;

        public Vector2 ActualOutput => actualOutput;
        public float ActualLateralOutput { get; private set; }
        public float ActualForwardSpeedMetresPerSecond { get; private set; }
        public float ActualForwardSpeedSceneUnitsPerSecond { get; private set; }
        public RobotMotionMode CurrentMode => target.Mode;
        public string LastAction { get; private set; }
        public string HeadPitchLevel { get; private set; } = "center";
        public string LastActionReceiptId { get; private set; }
        public string LastActionReceiptStatus { get; private set; }
        public string ActiveMirroredStepId => mirroredStepId;
        public string LastMirroredStepStatus { get; private set; }
        public string MirroredStepPhase => mirroredStepId == null ? LastMirroredStepStatus
            : stepElapsed >= stepDuration ? "waiting_ack" : "playing";

        public float Deadzone
        {
            get => deadzone;
            set => deadzone = Mathf.Clamp01(value);
        }

        public bool ContinuousMotion
        {
            get => continuousMotion;
            set => continuousMotion = value;
        }

        public float MovementSpeedMultiplier
        {
            get => movementSpeedMultiplier;
            set => movementSpeedMultiplier = Mathf.Max(0f, value);
        }

        public float LeftTurnMultiplier
        {
            get => leftTurnMultiplier;
            set => leftTurnMultiplier = Mathf.Max(0f, value);
        }

        public float RightTurnMultiplier
        {
            get => rightTurnMultiplier;
            set => rightTurnMultiplier = Mathf.Max(0f, value);
        }

        private void Reset()
        {
            body = GetComponent<Rigidbody>();
            body.interpolation = RigidbodyInterpolation.Interpolate;
            body.constraints |= RigidbodyConstraints.FreezeRotationX | RigidbodyConstraints.FreezeRotationZ;
        }

        private void Awake()
        {
            if (body == null)
            {
                body = GetComponent<Rigidbody>();
            }
            if (headPitchTransform == null)
            {
                headPitchTransform = transform.Find("Robot Camera");
            }
            if (enableCourseFireExtinguishAdapter
                && GetComponent<CourseFireExtinguishAdapter>() == null)
            {
                gameObject.AddComponent<CourseFireExtinguishAdapter>();
            }
        }

        private void FixedUpdate()
        {
            if (mirroredStepId != null)
            {
                UpdateMirroredStep(Time.fixedDeltaTime);
                return;
            }
            float velocity = target.Velocity;
            float steer = target.Steer;
            float lateral = target.Lateral;
            if (!continuousMotion && target.Mode != RobotMotionMode.Continuous)
            {
                RobotCommand discrete = TonyPiCommandMapper.ToCommand(velocity, steer, target.Grab, deadzone, false, lateral);
                velocity = discrete.Velocity;
                steer = discrete.Steer;
                lateral = discrete.Lateral;
            }

            Vector3 currentVelocity = body.velocity;
            float forwardSpeedMetresPerSecond = useMeasuredForwardTiers && continuousMotion
                ? CalculateMeasuredForwardSpeed(velocity)
                : velocity * maximumForwardSpeed;
            float forwardSpeedSceneUnitsPerSecond = forwardSpeedMetresPerSecond * movementSpeedMultiplier;
            Vector3 planarVelocity = transform.forward * forwardSpeedSceneUnitsPerSecond
                + transform.right * (lateral * maximumLateralSpeed * movementSpeedMultiplier);
            body.velocity = new Vector3(planarVelocity.x, currentVelocity.y, planarVelocity.z);
            body.angularVelocity = new Vector3(0f, CalculateTurnDegreesPerSecond(steer) * Mathf.Deg2Rad, 0f);
            actualOutput = new Vector2(velocity, steer);
            ActualForwardSpeedMetresPerSecond = forwardSpeedMetresPerSecond;
            ActualForwardSpeedSceneUnitsPerSecond = forwardSpeedSceneUnitsPerSecond;
            ActualLateralOutput = lateral;
        }

        private void OnDisable()
        {
            StopMotion();
            if (body != null)
            {
                Vector3 velocity = body.velocity;
                body.velocity = new Vector3(0f, velocity.y, 0f);
                body.angularVelocity = Vector3.zero;
            }
        }

        public void ApplyCommand(RobotCommand command)
        {
            if (command.IsMirroredAction)
            {
                LastActionReceiptId = command.ActionId;
                LastActionReceiptStatus = command.ActionStatus;
                return;
            }
            if (command.IsMirroredStep)
            {
                ApplyMirroredStep(command);
                return;
            }
            if (mirroredStepId != null)
            {
                StopMotion();
            }
            if (command.IsAction)
            {
                LastAction = command.Action;
                ApplyHeadAction(command.Action);
                if (string.Equals(command.Action, "stand", StringComparison.OrdinalIgnoreCase))
                {
                    StopMotion();
                }
                ActionReceived?.Invoke(command.Action);
                return;
            }

            target = continuousMotion
                ? TonyPiCommandMapper.ToCommand(command.Velocity, command.Steer, command.Grab, deadzone, true, command.Lateral)
                : TonyPiCommandMapper.ToCommand(command.Velocity, command.Steer, command.Grab, deadzone, false, command.Lateral);
            CommandApplied?.Invoke(target);
        }

        private void ApplyHeadAction(string action)
        {
            float pitch;
            switch (action)
            {
                case "head_down":
                    HeadPitchLevel = "down";
                    pitch = headDownPitchDegrees;
                    break;
                case "head_center":
                    HeadPitchLevel = "center";
                    pitch = headCenterPitchDegrees;
                    break;
                case "head_up":
                    HeadPitchLevel = "up";
                    pitch = headUpPitchDegrees;
                    break;
                default:
                    return;
            }
            if (headPitchTransform != null)
            {
                Vector3 angles = headPitchTransform.localEulerAngles;
                headPitchTransform.localRotation = Quaternion.Euler(pitch, angles.y, angles.z);
            }
        }

        public void StopMotion()
        {
            FinishMirroredStep("cancelled");
            target = RobotCommand.Stop;
            actualOutput = Vector2.zero;
            ActualForwardSpeedMetresPerSecond = 0f;
            ActualForwardSpeedSceneUnitsPerSecond = 0f;
            ActualLateralOutput = 0f;
            if (body != null)
            {
                body.velocity = new Vector3(0f, body.velocity.y, 0f);
                body.angularVelocity = Vector3.zero;
            }
            CommandApplied?.Invoke(target);
        }

        private void ApplyMirroredStep(RobotCommand command)
        {
            string ident = command.MirroredStepId;
            string phase = command.MirroredStepPhase;
            if (phase == "heartbeat")
            {
                return; // Transport watchdog only; never restart or extend the animation.
            }
            if (phase != "start")
            {
                if (seenStepIds.Count < 2048) seenStepIds.Add(ident);
                if (ident == mirroredStepId)
                {
                    FinishMirroredStep(phase);
                    StopMotion(); // No endpoint snap and no replay on late DONE.
                }
                return;
            }
            if (seenStepIds.Contains(ident)) return;
            if (seenStepIds.Count >= 2048)
            {
                StopMotion(); // Fail closed instead of evicting IDs and replaying old steps.
                return;
            }
            StopMotion();
            seenStepIds.Add(ident);
            mirroredStepId = ident;
            stepElapsed = 0f;
            stepStartedRealtime = Time.realtimeSinceStartup;
            bool turn = command.Steer != 0f;
            float magnitude = Mathf.Abs(turn ? command.Steer : command.Lateral);
            float sign = Mathf.Sign(turn ? command.Steer : command.Lateral);
            bool small = magnitude <= 0.45f;
            bool fast = magnitude > 0.75f;
            stepDegrees = turn ? sign * (small ? smallStepTurnDegrees : fast ? fastStepTurnDegrees : normalStepTurnDegrees) : 0f;
            stepMetres = turn ? 0f : sign * movementSpeedMultiplier
                * (small ? smallStepLateralMetres : fast ? fastStepLateralMetres : normalStepLateralMetres);
            stepDuration = Mathf.Max(0.1f, small
                ? (turn ? (sign < 0 ? smallLeftTurnSeconds : smallRightTurnSeconds)
                        : (sign < 0 ? smallLeftLateralSeconds : smallRightLateralSeconds))
                : normalStepSeconds);
            target = command;
            LastMirroredStepStatus = "start";
            CommandApplied?.Invoke(target);
            MirroredStepChanged?.Invoke(ident, "start");
        }

        private void UpdateMirroredStep(float deltaTime)
        {
            if (Time.realtimeSinceStartup - stepStartedRealtime >= maximumMirrorWaitSeconds)
            {
                FinishMirroredStep("timeout");
                StopMotion();
                return;
            }
            float before = Mathf.Clamp01(stepElapsed / stepDuration);
            stepElapsed += deltaTime;
            float after = Mathf.Clamp01(stepElapsed / stepDuration);
            // Smoothstep distributes one finite displacement across physics frames.
            // At the budget's end we hold still even if hardware DONE is delayed.
            float fractionPerSecond = (SmoothProgress(after) - SmoothProgress(before)) / Mathf.Max(deltaTime, 0.0001f);
            float turnRate = stepDegrees * fractionPerSecond;
            Vector3 lateralVelocity = transform.right * (stepMetres * fractionPerSecond);
            body.velocity = new Vector3(lateralVelocity.x, body.velocity.y, lateralVelocity.z);
            body.angularVelocity = new Vector3(0f, turnRate * Mathf.Deg2Rad, 0f);
            actualOutput = new Vector2(0f, maximumTurnDegreesPerSecond > 0f ? turnRate / maximumTurnDegreesPerSecond : 0f);
            ActualLateralOutput = maximumLateralSpeed > 0f ? stepMetres * fractionPerSecond / maximumLateralSpeed : 0f;
        }

        private static float SmoothProgress(float value) => value * value * (3f - 2f * value);

        public float CalculateMeasuredForwardSpeed(float velocity)
        {
            if (Mathf.Abs(velocity) <= deadzone)
            {
                return 0f;
            }

            float magnitude = Mathf.Abs(velocity);
            float multiplier = magnitude <= slowTierMaximum
                ? oneStepSpeedMultiplier
                : magnitude > fastTierMinimum
                    ? fastSpeedMultiplier
                    : 1f;
            return Mathf.Sign(velocity) * baselineForwardSpeed * multiplier;
        }

        public float CalculateTurnDegreesPerSecond(float steer)
        {
            float input = Mathf.Clamp(steer, -1f, 1f);
            if (Mathf.Abs(input) <= deadzone)
            {
                return 0f;
            }

            float baseRate = maximumTurnDegreesPerSecond;
            if (useMeasuredFullStickTurn)
            {
                float cycleSeconds = fullStickTurnActionSeconds + turnRepeatIntervalSeconds;
                baseRate = cycleSeconds > 0f ? fullStickTurnDegreesPerAction / cycleSeconds : 0f;
            }
            float directionMultiplier = input < 0f ? leftTurnMultiplier : rightTurnMultiplier;
            return input * baseRate * directionMultiplier;
        }

        private void FinishMirroredStep(string status)
        {
            if (mirroredStepId == null) return;
            string ident = mirroredStepId;
            mirroredStepId = null;
            LastMirroredStepStatus = status;
            MirroredStepChanged?.Invoke(ident, status);
        }
    }
}
