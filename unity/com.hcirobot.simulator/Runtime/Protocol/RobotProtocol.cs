using System;
using System.Globalization;
using System.Text;
using System.Text.RegularExpressions;
using UnityEngine;

namespace HciRobot.Simulator
{
    public enum RobotMotionMode
    {
        Stand,
        Forward,
        Backward,
        TurnLeft,
        TurnRight,
        Continuous,
        LateralLeft,
        LateralRight
    }

    [Serializable]
    public sealed class RobotControlMessage
    {
        public float v;
        public float steer;
        public float lateral;
        public bool grab;
        public string t;
    }

    [Serializable]
    public sealed class MirroredStepMessage
    {
        public string id;
        public string phase;
        public float steer;
        public float lateral;
    }

    public readonly struct RobotCommand
    {
        public RobotCommand(float velocity, float steer, bool grab, string action, RobotMotionMode mode,
            float lateral = 0f, string mirroredStepId = null, string mirroredStepPhase = null)
        {
            Velocity = velocity;
            Steer = steer;
            Lateral = lateral;
            Grab = grab;
            Action = action;
            Mode = mode;
            MirroredStepId = mirroredStepId;
            MirroredStepPhase = mirroredStepPhase;
        }

        public float Velocity { get; }
        public float Steer { get; }
        public float Lateral { get; }
        public bool Grab { get; }
        public string Action { get; }
        public RobotMotionMode Mode { get; }
        public bool IsAction => !string.IsNullOrEmpty(Action);
        public string MirroredStepId { get; }
        public string MirroredStepPhase { get; }
        public bool IsMirroredStep => !string.IsNullOrEmpty(MirroredStepId);

        public static RobotCommand Stop => new RobotCommand(0f, 0f, false, null, RobotMotionMode.Stand);
    }

    public static class TonyPiCommandMapper
    {
        public static RobotMotionMode MapDiscrete(float velocity, float steer, float deadzone = 0.20f, float lateral = 0f)
        {
            deadzone = Mathf.Clamp01(deadzone);
            if (Mathf.Abs(steer) > deadzone)
            {
                return steer > 0f ? RobotMotionMode.TurnRight : RobotMotionMode.TurnLeft;
            }

            if (Mathf.Abs(lateral) > deadzone)
            {
                return lateral > 0f ? RobotMotionMode.LateralRight : RobotMotionMode.LateralLeft;
            }

            if (Mathf.Abs(velocity) > deadzone)
            {
                return velocity > 0f ? RobotMotionMode.Forward : RobotMotionMode.Backward;
            }

            return RobotMotionMode.Stand;
        }

        public static RobotCommand ToCommand(
            float velocity,
            float steer,
            bool grab,
            float deadzone,
            bool continuous,
            float lateral = 0f)
        {
            if (!IsFinite(velocity) || !IsFinite(steer) || !IsFinite(lateral))
            {
                return RobotCommand.Stop;
            }

            velocity = Mathf.Clamp(velocity, -1f, 1f);
            steer = Mathf.Clamp(steer, -1f, 1f);
            lateral = Mathf.Clamp(lateral, -1f, 1f);
            if (continuous)
            {
                // Lateral requests use the robot's exclusive gait priority.
                // Existing two-axis continuous commands retain their semantics.
                if (Mathf.Abs(lateral) > deadzone)
                {
                    return Mathf.Abs(steer) > deadzone
                        ? new RobotCommand(0f, steer, grab, null, RobotMotionMode.Continuous)
                        : new RobotCommand(0f, 0f, grab, null, RobotMotionMode.Continuous, lateral);
                }
                if (Mathf.Abs(velocity) <= deadzone)
                {
                    velocity = 0f;
                }
                if (Mathf.Abs(steer) <= deadzone)
                {
                    steer = 0f;
                }
                return new RobotCommand(velocity, steer, grab, null, RobotMotionMode.Continuous);
            }

            RobotMotionMode mode = MapDiscrete(velocity, steer, deadzone, lateral);
            switch (mode)
            {
                case RobotMotionMode.Forward:
                    return new RobotCommand(1f, 0f, grab, null, mode);
                case RobotMotionMode.Backward:
                    return new RobotCommand(-1f, 0f, grab, null, mode);
                case RobotMotionMode.TurnLeft:
                    return new RobotCommand(0f, -1f, grab, null, mode);
                case RobotMotionMode.TurnRight:
                    return new RobotCommand(0f, 1f, grab, null, mode);
                case RobotMotionMode.LateralLeft:
                    return new RobotCommand(0f, 0f, grab, null, mode, -1f);
                case RobotMotionMode.LateralRight:
                    return new RobotCommand(0f, 0f, grab, null, mode, 1f);
                default:
                    return new RobotCommand(0f, 0f, grab, null, RobotMotionMode.Stand);
            }
        }

        public static RobotCommand Action(string name)
        {
            return new RobotCommand(0f, 0f, false, name, RobotMotionMode.Stand);
        }

        private static bool IsFinite(float value)
        {
            return !float.IsNaN(value) && !float.IsInfinity(value);
        }
    }

    public sealed class JsonLineAccumulator
    {
        private readonly int maxBufferedBytes;
        private byte[] buffer;
        private int count;

        public JsonLineAccumulator(int maxBufferedBytes = 64 * 1024)
        {
            this.maxBufferedBytes = Math.Max(256, maxBufferedBytes);
            buffer = new byte[Math.Min(4096, this.maxBufferedBytes)];
        }

        public void Append(byte[] source, int length, Action<string> onLine)
        {
            if (source == null || onLine == null || length <= 0)
            {
                return;
            }

            int safeLength = Math.Min(length, source.Length);
            if (safeLength > maxBufferedBytes || count + safeLength > maxBufferedBytes)
            {
                count = 0;
                return;
            }

            EnsureCapacity(count + safeLength);
            Buffer.BlockCopy(source, 0, buffer, count, safeLength);
            count += safeLength;

            int lineStart = 0;
            for (int i = 0; i < count; i++)
            {
                if (buffer[i] != (byte)'\n')
                {
                    continue;
                }

                int lineLength = i - lineStart;
                if (lineLength > 0 && buffer[i - 1] == (byte)'\r')
                {
                    lineLength--;
                }

                string line = Encoding.UTF8.GetString(buffer, lineStart, lineLength);
                onLine(line);
                lineStart = i + 1;
            }

            if (lineStart > 0)
            {
                int remaining = count - lineStart;
                if (remaining > 0)
                {
                    Buffer.BlockCopy(buffer, lineStart, buffer, 0, remaining);
                }
                count = remaining;
            }
        }

        public void Reset()
        {
            count = 0;
        }

        private void EnsureCapacity(int required)
        {
            if (required <= buffer.Length)
            {
                return;
            }

            int size = buffer.Length;
            while (size < required)
            {
                size = Math.Min(maxBufferedBytes, size * 2);
            }
            Array.Resize(ref buffer, size);
        }
    }

    public static class RobotProtocolParser
    {
        public static bool TryParseLine(
            string line,
            float deadzone,
            bool continuous,
            out RobotCommand command)
        {
            command = RobotCommand.Stop;
            if (string.IsNullOrWhiteSpace(line))
            {
                return false;
            }

            string text = line.Trim();
            if (text.StartsWith("MIRROR_STEP:", StringComparison.Ordinal))
            {
                return TryParseMirroredStep(text.Substring(12), deadzone, out command);
            }
            if (text.StartsWith("CMD:", StringComparison.Ordinal))
            {
                string action = text.Substring(4).Trim();
                if (!IsValidAction(action))
                {
                    return false;
                }
                command = TonyPiCommandMapper.Action(action.ToLowerInvariant());
                return true;
            }

            try
            {
                RobotControlMessage dto = JsonUtility.FromJson<RobotControlMessage>(text);
                if (dto == null || !HasRequiredNumericFields(text))
                {
                    return false;
                }

                if (!IsFinite(dto.v) || !IsFinite(dto.steer) || !IsFinite(dto.lateral)
                    || dto.v < -1f || dto.v > 1f || dto.steer < -1f || dto.steer > 1f
                    || dto.lateral < -1f || dto.lateral > 1f)
                {
                    return false;
                }

                command = TonyPiCommandMapper.ToCommand(dto.v, dto.steer, dto.grab, deadzone, continuous, dto.lateral);
                return true;
            }
            catch (ArgumentException)
            {
                return false;
            }
        }

        private static bool HasRequiredNumericFields(string json)
        {
            return ContainsJsonKey(json, "v") && ContainsJsonKey(json, "steer");
        }

        private static bool TryParseMirroredStep(string json, float deadzone, out RobotCommand command)
        {
            command = RobotCommand.Stop;
            try
            {
                var dto = JsonUtility.FromJson<MirroredStepMessage>(json);
                if (dto == null || dto.id == null || !Regex.IsMatch(dto.id, @"\A[A-Za-z0-9_-]{1,64}\z"))
                {
                    return false;
                }
                if (dto.phase != "start" && dto.phase != "heartbeat" && dto.phase != "done"
                    && dto.phase != "cancelled" && dto.phase != "error")
                {
                    return false;
                }
                if (dto.phase == "start")
                {
                    if (!HasNumericField(json, "steer") || !HasNumericField(json, "lateral")
                        || !IsFinite(dto.steer) || !IsFinite(dto.lateral)
                        || Mathf.Abs(dto.steer) > 1f || Mathf.Abs(dto.lateral) > 1f
                        || !((Mathf.Abs(dto.steer) > deadzone && dto.lateral == 0f)
                             || (Mathf.Abs(dto.lateral) > deadzone && dto.steer == 0f)))
                    {
                        return false;
                    }
                }
                command = new RobotCommand(0f, dto.steer, false, null, RobotMotionMode.Continuous,
                    dto.lateral, dto.id, dto.phase);
                return true;
            }
            catch (ArgumentException)
            {
                return false;
            }
        }

        private static bool HasNumericField(string json, string key)
        {
            return Regex.IsMatch(json, "\"" + key + "\"\\s*:\\s*-?(?:0|[1-9][0-9]*)(?:\\.[0-9]+)?(?:[eE][+-]?[0-9]+)?\\s*[,}]");
        }

        private static bool ContainsJsonKey(string json, string key)
        {
            return json.IndexOf("\"" + key + "\"", StringComparison.Ordinal) >= 0;
        }

        private static bool IsValidAction(string action)
        {
            if (string.IsNullOrEmpty(action) || action.Length > 32)
            {
                return false;
            }

            for (int i = 0; i < action.Length; i++)
            {
                char c = action[i];
                if (!(char.IsLetterOrDigit(c) || c == '_') || c > 127)
                {
                    return false;
                }
            }
            return true;
        }

        private static bool IsFinite(float value)
        {
            return !float.IsNaN(value) && !float.IsInfinity(value);
        }
    }

    public static class RobotTelemetryFormatter
    {
        public static byte[] DistanceLine(int millimetres)
        {
            string text = "DIST:" + Math.Max(0, millimetres).ToString(CultureInfo.InvariantCulture) + "\n";
            return Encoding.ASCII.GetBytes(text);
        }
    }
}
