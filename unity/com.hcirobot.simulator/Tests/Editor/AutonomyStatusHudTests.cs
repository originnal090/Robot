using System;
using NUnit.Framework;
using UnityEngine;

namespace HciRobot.Simulator.Tests
{
    public sealed class AutonomyStatusHudTests
    {
        private static AutonomyStatusMessage BuildMessage()
        {
            return new AutonomyStatusMessage
            {
                type = AutonomyStatusUdpReceiver.DefaultType,
                schema_version = 1,
                source = "hcirobot",
                session_id = "session",
                seq = 1,
                sent_at_ms = 1000,
                mode = "autonomy",
                armed = true,
                control_state = "APPROACHING",
                video_status = "streaming",
                frame_count = 10,
                target = new AutonomyTargetStatus
                {
                    candidate = true,
                    confirmed = true,
                    fresh = true,
                    age_ms = 40,
                    center_x = 0.52f,
                    center_y = 0.48f,
                    radius = 0.06f,
                    frame_width = 640,
                    frame_height = 480
                },
                output = new AutonomyOutputStatus
                {
                    v = 0.35f,
                    steer = -0.2f,
                    grab = false,
                    source = "autonomy"
                },
                obstacle = new AutonomyObstacleStatus
                {
                    state = "CLEAR",
                    distance_mm = 850f,
                    avoid_count = 0
                },
                estop = false,
                fault = "",
                termination = "",
                @event = null
            };
        }

        [Test]
        public void Format_ShowsStateTargetOutputMotionObstacle()
        {
            var lines = AutonomyStatusHudText.Format(
                BuildMessage(), true, RobotMotionMode.Forward, null);

            Assert.IsTrue(lines.Exists(l => l.Contains("state=APPROACHING") && l.Contains("armed=True")));
            Assert.IsTrue(lines.Exists(l => l.Contains("target=confirmed/fresh")));
            Assert.IsTrue(lines.Exists(l => l.Contains("output v=+0.35 steer=-0.20 [autonomy]")));
            Assert.IsTrue(lines.Exists(l => l.Contains("motion=Forward")));
            Assert.IsTrue(lines.Exists(l => l.Contains("obstacle=CLEAR") && l.Contains("dist=850mm")));
            Assert.IsFalse(lines.Exists(l => l.Contains("STALE")));
            Assert.IsFalse(lines.Exists(l => l.Contains("ESTOP")));
        }

        [Test]
        public void Format_ShowsStaleMarkerWhenNotFresh()
        {
            var lines = AutonomyStatusHudText.Format(
                BuildMessage(), false, RobotMotionMode.Stand, "stand");

            Assert.IsTrue(lines.Exists(l => l.Contains("STALE")));
            Assert.IsTrue(lines.Exists(l => l.Contains("(last CMD:stand)")));
        }

        [Test]
        public void Format_ShowsEstopFaultTerminationAndEvent()
        {
            var message = BuildMessage();
            message.estop = true;
            message.fault = "video_timeout";
            message.termination = "estop";
            message.@event = new AutonomyEventStatus { kind = "estop", message = "operator stop" };

            var lines = AutonomyStatusHudText.Format(message, true, RobotMotionMode.Stand, null);

            Assert.IsTrue(lines.Exists(l => l.Contains("ESTOP active")));
            Assert.IsTrue(lines.Exists(l => l.Contains("fault=video_timeout")));
            Assert.IsTrue(lines.Exists(l => l.Contains("termination=estop")));
            Assert.IsTrue(lines.Exists(l => l.Contains("event=operator stop")));
        }

        [Test]
        public void Format_NullMessageWaitsForStatus()
        {
            var lines = AutonomyStatusHudText.Format(null, false, RobotMotionMode.Stand, null);

            Assert.AreEqual(1, lines.Count);
            StringAssert.Contains("waiting", lines[0]);
        }

        [Test]
        public void Format_NegativeDistanceOmittedFromObstacleLine()
        {
            var message = BuildMessage();
            message.obstacle = new AutonomyObstacleStatus { state = "UNKNOWN", distance_mm = -1f, avoid_count = 0 };

            var lines = AutonomyStatusHudText.Format(message, true, RobotMotionMode.Stand, null);

            Assert.IsTrue(lines.Exists(l => l.Contains("obstacle=UNKNOWN") && !l.Contains("dist=")));
        }
    }
}
