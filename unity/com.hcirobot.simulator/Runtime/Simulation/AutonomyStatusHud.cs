using System.Collections.Generic;
using UnityEngine;

namespace HciRobot.Simulator
{
    /// <summary>
    /// Formats autonomy status into ASCII HUD lines, separated from OnGUI so it is unit-testable.
    /// </summary>
    public static class AutonomyStatusHudText
    {
        public static List<string> Format(
            AutonomyStatusMessage message,
            bool statusIsFresh,
            RobotMotionMode motionMode,
            string lastAction)
        {
            var lines = new List<string>();
            if (message == null)
            {
                lines.Add("autonomy: waiting for status...");
                return lines;
            }

            lines.Add($"state={message.control_state} mode={message.mode} armed={message.armed}");
            if (!statusIsFresh)
            {
                lines.Add("status=STALE (no UDP packets recently)");
            }

            if (message.target != null)
            {
                string targetState = message.target.confirmed ? "confirmed" :
                    (message.target.candidate ? "candidate" : "none");
                string freshness = message.target.fresh ? "fresh" : "stale";
                lines.Add(
                    $"target={targetState}/{freshness} center=({message.target.center_x:F2},{message.target.center_y:F2}) radius={message.target.radius:F3}");
            }

            if (message.output != null)
            {
                lines.Add($"output v={message.output.v:+0.00;-0.00} steer={message.output.steer:+0.00;-0.00} [{message.output.source}]");
            }

            string action = motionMode.ToString();
            if (!string.IsNullOrEmpty(lastAction))
            {
                action += $" (last CMD:{lastAction})";
            }
            lines.Add($"motion={action}");

            if (message.obstacle != null)
            {
                string obstacle = $"obstacle={message.obstacle.state}";
                if (message.obstacle.distance_mm >= 0f)
                {
                    obstacle += $" dist={message.obstacle.distance_mm:F0}mm";
                }
                obstacle += $" avoid={message.obstacle.avoid_count}";
                lines.Add(obstacle);
            }

            if (message.estop)
            {
                lines.Add("ESTOP active");
            }
            if (!string.IsNullOrEmpty(message.fault))
            {
                lines.Add($"fault={message.fault}");
            }
            if (!string.IsNullOrEmpty(message.termination))
            {
                lines.Add($"termination={message.termination}");
            }
            if (message.@event != null && !string.IsNullOrEmpty(message.@event.message))
            {
                lines.Add($"event={message.@event.message}");
            }
            return lines;
        }
    }

    /// <summary>
    /// Minimal IMGUI overlay showing the live autonomy status next to the simulation.
    /// Attach next to an AutonomyStatusUdpReceiver; optionally reference the motion driver.
    /// </summary>
    [DisallowMultipleComponent]
    public sealed class AutonomyStatusHud : MonoBehaviour
    {
        [SerializeField] private AutonomyStatusUdpReceiver statusReceiver;
        [SerializeField] private TonyPiMotionDriver motionDriver;
        [SerializeField, Min(8)] private int fontSize = 15;

        private readonly List<string> lines = new List<string>();
        private bool receiverIsFresh;
        private GUIContent content = new GUIContent();
        private GUIStyle boxStyle;
        private GUIStyle labelStyle;
        private float boxHeight;

        public void Configure(AutonomyStatusUdpReceiver receiver, TonyPiMotionDriver driver)
        {
            statusReceiver = receiver;
            motionDriver = driver;
        }

        private void OnEnable()
        {
            if (statusReceiver != null)
            {
                statusReceiver.StatusUpdated += HandleStatusUpdated;
                statusReceiver.StatusTimedOut += HandleStatusTimedOut;
                receiverIsFresh = statusReceiver.IsFresh;
            }
        }

        private void OnDisable()
        {
            if (statusReceiver != null)
            {
                statusReceiver.StatusUpdated -= HandleStatusUpdated;
                statusReceiver.StatusTimedOut -= HandleStatusTimedOut;
            }
        }

        private void HandleStatusUpdated(AutonomyStatusMessage message)
        {
            receiverIsFresh = true;
        }

        private void HandleStatusTimedOut()
        {
            receiverIsFresh = false;
        }

        private void Update()
        {
            // Poll IsFresh so the HUD reflects reality even between events.
            if (statusReceiver != null)
            {
                receiverIsFresh = statusReceiver.IsFresh;
            }
        }

        private void OnGUI()
        {
            if (boxStyle == null)
            {
                boxStyle = new GUIStyle(GUI.skin.box)
                {
                    alignment = TextAnchor.UpperLeft,
                    normal = { textColor = Color.white }
                };
                labelStyle = new GUIStyle(GUI.skin.label)
                {
                    fontSize = fontSize,
                    alignment = TextAnchor.UpperLeft,
                    normal = { textColor = Color.white }
                };
            }

            lines.Clear();
            AutonomyStatusMessage message = statusReceiver != null ? statusReceiver.Latest : null;
            lines.AddRange(AutonomyStatusHudText.Format(
                message,
                receiverIsFresh,
                motionDriver != null ? motionDriver.CurrentMode : RobotMotionMode.Stand,
                motionDriver != null ? motionDriver.LastAction : null));

            content.text = string.Join("\n", lines);
            Vector2 size = labelStyle.CalcSize(content);
            float padding = 8f;
            boxHeight = size.y + padding * 2f;
            GUI.Box(new Rect(10f, 10f, size.x + padding * 2f, boxHeight), string.Empty, boxStyle);
            GUI.Label(new Rect(10f + padding, 10f + padding, size.x, size.y), content, labelStyle);
        }
    }
}
