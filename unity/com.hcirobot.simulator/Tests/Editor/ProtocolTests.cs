using System.Collections.Generic;
using System.Text;
using System.Reflection;
using System.Net.Sockets;
using NUnit.Framework;
using UnityEngine;

namespace HciRobot.Simulator.Tests
{
    public sealed class ProtocolTests
    {
        [Test]
        public void JsonDtoSchema_ParsesRequiredTonyPiFields()
        {
            const string json = "{\"v\":0.4,\"steer\":-0.3,\"grab\":true,\"t\":\"2026-09-12T00:00:00Z\"}";

            bool accepted = RobotProtocolParser.TryParseLine(json, 0.20f, true, out RobotCommand command);

            Assert.That(accepted, Is.True);
            Assert.That(command.Velocity, Is.EqualTo(0.4f).Within(0.0001f));
            Assert.That(command.Steer, Is.EqualTo(-0.3f).Within(0.0001f));
            Assert.That(command.Grab, Is.True);
            Assert.That(command.Lateral, Is.Zero);
        }

        [TestCase(-0.35f)]
        [TestCase(0.35f)]
        public void JsonDtoSchema_ParsesOptionalLateral(float lateral)
        {
            string json = "{\"v\":0,\"steer\":0,\"lateral\":"
                + lateral.ToString(System.Globalization.CultureInfo.InvariantCulture) + "}";
            Assert.That(RobotProtocolParser.TryParseLine(json, 0.2f, true, out RobotCommand command), Is.True);
            Assert.That(command.Lateral, Is.EqualTo(lateral));
            Assert.That(command.Steer, Is.Zero);
            Assert.That(command.Velocity, Is.Zero);
            Assert.That(RobotProtocolParser.TryParseLine("{\"v\":0,\"steer\":0,\"lateral\":1.1}", 0.2f, true, out _), Is.False);
            Assert.That(RobotProtocolParser.TryParseLine("{\"v\":0,\"steer\":0,\"lateral\":-1.1}", 0.2f, true, out _), Is.False);
        }

        [Test]
        public void JsonDtoSchema_RejectsMissingOrOutOfRangeFields()
        {
            Assert.That(RobotProtocolParser.TryParseLine("{\"v\":0.2}", 0.20f, false, out _), Is.False);
            Assert.That(RobotProtocolParser.TryParseLine("{\"v\":2,\"steer\":0}", 0.20f, false, out _), Is.False);
            Assert.That(RobotProtocolParser.TryParseLine("not-json", 0.20f, false, out _), Is.False);
        }

        [Test]
        public void Accumulator_PreservesPartialLinesAcrossReads()
        {
            var accumulator = new JsonLineAccumulator();
            var lines = new List<string>();
            byte[] first = Encoding.UTF8.GetBytes("{\"v\":0.3,\"steer\":0}\nCMD:no");
            byte[] second = Encoding.UTF8.GetBytes("d\n");

            accumulator.Append(first, first.Length, lines.Add);
            accumulator.Append(second, second.Length, lines.Add);

            Assert.That(lines, Is.EqualTo(new[] { "{\"v\":0.3,\"steer\":0}", "CMD:nod" }));
        }

        [Test]
        public void DistanceTelemetry_IsSingleAsciiLine()
        {
            Assert.That(Encoding.ASCII.GetString(RobotTelemetryFormatter.DistanceLine(317)), Is.EqualTo("DIST:317\n"));
        }

        [Test]
        public void CommandDiagnostics_CollapseMagnitudeChangesButDescribeCurrentValues()
        {
            var leftSlow = new RobotCommand(0f, -0.35f, false, null, RobotMotionMode.Continuous);
            var leftFast = new RobotCommand(0f, -0.90f, false, null, RobotMotionMode.Continuous);
            var forwardLeft = new RobotCommand(0.4f, -0.2f, false, null, RobotMotionMode.Continuous);

            Assert.That(RobotCommandDiagnostics.ChangeKey(leftSlow), Is.EqualTo("motion:turn-left"));
            Assert.That(RobotCommandDiagnostics.ChangeKey(leftFast), Is.EqualTo("motion:turn-left"));
            Assert.That(RobotCommandDiagnostics.ChangeKey(forwardLeft), Is.EqualTo("motion:forward+left"));
            Assert.That(RobotCommandDiagnostics.Describe(leftSlow),
                Is.EqualTo("motion=turn-left v=0.00 steer=-0.35 lateral=0.00"));
            Assert.That(RobotCommandDiagnostics.Describe(RobotCommand.Stop),
                Is.EqualTo("motion=stand v=0.00 steer=0.00 lateral=0.00"));
        }

        [Test]
        public void ActionV1_ParsesRequestAndFormatsCorrelatedReceipts()
        {
            const string line = "ACTION:{\"id\":\"action_1\",\"name\":\"head_down\"}";
            Assert.That(RobotProtocolParser.TryParseLine(line, 0.2f, false, out RobotCommand command), Is.True);
            Assert.That(command.IsActionRequest, Is.True);
            Assert.That(command.ActionId, Is.EqualTo("action_1"));
            Assert.That(command.Action, Is.EqualTo("head_down"));
            Assert.That(Encoding.ASCII.GetString(RobotTelemetryFormatter.CapabilitiesLine()),
                Is.EqualTo("CAPS:ACTION_V1\n"));
            Assert.That(Encoding.UTF8.GetString(RobotTelemetryFormatter.ActionStatusLine(
                "action_1", "head_down", "done")),
                Is.EqualTo("ACTION_STATUS:{\"id\":\"action_1\",\"name\":\"head_down\","
                    + "\"status\":\"done\",\"detail\":\"\"}\n"));
        }

        [TestCase("MIRROR_ACTION:{\"id\":\"a1\",\"name\":\"head_up\",\"status\":\"accepted\"}", "accepted")]
        [TestCase("MIRROR_ACTION:{\"id\":\"a1\",\"name\":\"head_up\",\"status\":\"done\"}", "done")]
        public void MirroredAction_ParsesRealRobotReceipt(string line, string status)
        {
            Assert.That(RobotProtocolParser.TryParseLine(line, 0.2f, false, out RobotCommand command), Is.True);
            Assert.That(command.IsMirroredAction, Is.True);
            Assert.That(command.ActionId, Is.EqualTo("a1"));
            Assert.That(command.ActionStatus, Is.EqualTo(status));
        }

        [TestCase("ACTION:{\"id\":\"bad id\",\"name\":\"head_up\"}")]
        [TestCase("ACTION:{\"id\":\"a1\",\"name\":\"bad/name\"}")]
        [TestCase("MIRROR_ACTION:{\"id\":\"a1\",\"name\":\"head_up\",\"status\":\"pending\"}")]
        public void ActionV1_RejectsMalformedMessages(string line)
        {
            Assert.That(RobotProtocolParser.TryParseLine(line, 0.2f, false, out _), Is.False);
        }

        [Test]
        public void MirroredStep_StartPreservesGaitMagnitudeInDiscreteMode()
        {
            const string line = "MIRROR_STEP:{\"id\":\"step_1\",\"phase\":\"start\",\"steer\":-0.35,\"lateral\":0}";
            Assert.That(RobotProtocolParser.TryParseLine(line, 0.2f, false, out RobotCommand command), Is.True);
            Assert.That(command.IsMirroredStep, Is.True);
            Assert.That(command.MirroredStepId, Is.EqualTo("step_1"));
            Assert.That(command.MirroredStepPhase, Is.EqualTo("start"));
            Assert.That(command.Steer, Is.EqualTo(-0.35f));
            Assert.That(command.IsAction, Is.False);
        }

        [TestCase("heartbeat")]
        [TestCase("done")]
        [TestCase("cancelled")]
        [TestCase("error")]
        public void MirroredStep_ParsesLifecycleWithoutMotionFields(string phase)
        {
            string line = "MIRROR_STEP:{\"id\":\"step_1\",\"phase\":\"" + phase + "\"}";
            Assert.That(RobotProtocolParser.TryParseLine(line, 0.2f, false, out RobotCommand command), Is.True);
            Assert.That(command.MirroredStepPhase, Is.EqualTo(phase));
            Assert.That(command.Velocity, Is.Zero);
        }

        [TestCase("{\"id\":\"a\",\"phase\":\"unknown\"}")]
        [TestCase("{\"id\":\"bad id\",\"phase\":\"done\"}")]
        [TestCase("{\"id\":\"a\",\"phase\":\"start\"}")]
        [TestCase("{\"id\":\"a\",\"phase\":\"start\",\"steer\":0.4,\"lateral\":0.3}")]
        [TestCase("{\"id\":\"a\",\"phase\":\"start\",\"steer\":0.4,\"lateral\":true}")]
        [TestCase("{\"id\":\"a\",\"phase\":\"start\",\"steer\":2,\"lateral\":0}")]
        public void MirroredStep_RejectsMalformedEvents(string json)
        {
            Assert.That(RobotProtocolParser.TryParseLine("MIRROR_STEP:" + json, 0.2f, false, out _), Is.False);
        }

        [Test]
        public void MirroredStep_ServerQueuesOnMainThreadAndWatchdogStopsIt()
        {
            var robot = new GameObject("mirror transport test");
            robot.SetActive(false); // No real listener in this deterministic transport test.
            var source = new TcpClient();
            try
            {
                robot.AddComponent<Rigidbody>();
                var driver = robot.AddComponent<TonyPiMotionDriver>();
                var server = robot.AddComponent<VirtualRobotTcpServer>();
                var flags = BindingFlags.Instance | BindingFlags.NonPublic;
                typeof(TonyPiMotionDriver).GetMethod("Awake", flags).Invoke(driver, null);
                typeof(VirtualRobotTcpServer).GetField("motionDriver", flags).SetValue(server, driver);
                typeof(VirtualRobotTcpServer).GetField("client", flags).SetValue(server, source);
                var handle = typeof(VirtualRobotTcpServer).GetMethod("HandleLine", flags);
                var update = typeof(VirtualRobotTcpServer).GetMethod("Update", flags);
                handle.Invoke(server, new object[] { source,
                    "MIRROR_STEP:{\"id\":\"one\",\"phase\":\"start\",\"steer\":0.35,\"lateral\":0}" });
                Assert.That(driver.ActiveMirroredStepId, Is.Null);
                update.Invoke(server, null);
                Assert.That(driver.ActiveMirroredStepId, Is.EqualTo("one"));
                var lastTicks = typeof(VirtualRobotTcpServer).GetField("lastValidCommandTicks", flags);
                lastTicks.SetValue(server, 1L);
                handle.Invoke(server, new object[] { source, "MIRROR_STEP:{\"id\":\"wrong\",\"phase\":\"heartbeat\"}" });
                Assert.That(lastTicks.GetValue(server), Is.EqualTo(1L));
                handle.Invoke(server, new object[] { source, "MIRROR_STEP:{\"id\":\"one\",\"phase\":\"heartbeat\"}" });
                update.Invoke(server, null);
                Assert.That(driver.ActiveMirroredStepId, Is.EqualTo("one"));
                lastTicks.SetValue(server, 1L);
                update.Invoke(server, null);
                Assert.That(driver.ActiveMirroredStepId, Is.Null);
                Assert.That(driver.LastMirroredStepStatus, Is.EqualTo("cancelled"));
            }
            finally
            {
                source.Close();
                Object.DestroyImmediate(robot);
            }
        }

        [Test]
        public void ActionV1_ServerAppliesOnMainThreadAndDeduplicatesId()
        {
            var robot = new GameObject("action transport test");
            robot.SetActive(false);
            var source = new TcpClient();
            try
            {
                robot.AddComponent<Rigidbody>();
                var camera = new GameObject("Robot Camera");
                camera.transform.SetParent(robot.transform, false);
                var driver = robot.AddComponent<TonyPiMotionDriver>();
                var server = robot.AddComponent<VirtualRobotTcpServer>();
                var flags = BindingFlags.Instance | BindingFlags.NonPublic;
                typeof(TonyPiMotionDriver).GetMethod("Awake", flags).Invoke(driver, null);
                typeof(VirtualRobotTcpServer).GetField("motionDriver", flags).SetValue(server, driver);
                typeof(VirtualRobotTcpServer).GetField("client", flags).SetValue(server, source);
                var handle = typeof(VirtualRobotTcpServer).GetMethod("HandleLine", flags);
                var update = typeof(VirtualRobotTcpServer).GetMethod("Update", flags);

                handle.Invoke(server, new object[] { source,
                    "ACTION:{\"id\":\"same_id\",\"name\":\"head_down\"}" });
                Assert.That(driver.LastAction, Is.Null);
                update.Invoke(server, null);
                Assert.That(driver.LastAction, Is.EqualTo("head_down"));
                Assert.That(driver.HeadPitchLevel, Is.EqualTo("down"));

                handle.Invoke(server, new object[] { source,
                    "ACTION:{\"id\":\"same_id\",\"name\":\"head_up\"}" });
                update.Invoke(server, null);
                Assert.That(driver.LastAction, Is.EqualTo("head_down"));
                var history = (Dictionary<string, string>)typeof(VirtualRobotTcpServer)
                    .GetField("actionHistory", flags).GetValue(server);
                Assert.That(history["same_id"], Is.EqualTo("done"));
            }
            finally
            {
                source.Close();
                Object.DestroyImmediate(robot);
            }
        }
    }
}
