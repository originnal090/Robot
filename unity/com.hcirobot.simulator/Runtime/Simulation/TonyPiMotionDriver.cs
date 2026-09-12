using System;
using UnityEngine;

namespace HciRobot.Simulator
{
    [DisallowMultipleComponent]
    [RequireComponent(typeof(Rigidbody))]
    public sealed class TonyPiMotionDriver : MonoBehaviour
    {
        [SerializeField] private Rigidbody body;
        [SerializeField, Min(0f)] private float maximumForwardSpeed = 1.0f;
        [SerializeField, Min(0f)] private float maximumTurnDegreesPerSecond = 120f;
        [SerializeField, Range(0f, 1f)] private float deadzone = 0.20f;
        [SerializeField] private bool continuousMotion;

        private RobotCommand target = RobotCommand.Stop;
        private Vector2 actualOutput;

        public event Action<RobotCommand> CommandApplied;
        public event Action<string> ActionReceived;

        public Vector2 ActualOutput => actualOutput;
        public RobotMotionMode CurrentMode => target.Mode;
        public string LastAction { get; private set; }

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
        }

        private void FixedUpdate()
        {
            float velocity = target.Velocity;
            float steer = target.Steer;
            if (!continuousMotion && target.Mode != RobotMotionMode.Continuous)
            {
                RobotCommand discrete = TonyPiCommandMapper.ToCommand(velocity, steer, target.Grab, deadzone, false);
                velocity = discrete.Velocity;
                steer = discrete.Steer;
            }

            Vector3 currentVelocity = body.velocity;
            Vector3 planarVelocity = transform.forward * (velocity * maximumForwardSpeed);
            body.velocity = new Vector3(planarVelocity.x, currentVelocity.y, planarVelocity.z);
            body.angularVelocity = new Vector3(0f, steer * maximumTurnDegreesPerSecond * Mathf.Deg2Rad, 0f);
            actualOutput = new Vector2(velocity, steer);
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
            if (command.IsAction)
            {
                LastAction = command.Action;
                if (string.Equals(command.Action, "stand", StringComparison.OrdinalIgnoreCase))
                {
                    StopMotion();
                }
                ActionReceived?.Invoke(command.Action);
                return;
            }

            target = continuousMotion
                ? TonyPiCommandMapper.ToCommand(command.Velocity, command.Steer, command.Grab, deadzone, true)
                : TonyPiCommandMapper.ToCommand(command.Velocity, command.Steer, command.Grab, deadzone, false);
            CommandApplied?.Invoke(target);
        }

        public void StopMotion()
        {
            target = RobotCommand.Stop;
            actualOutput = Vector2.zero;
            CommandApplied?.Invoke(target);
        }
    }
}
