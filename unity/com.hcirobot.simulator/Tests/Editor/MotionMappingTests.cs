using NUnit.Framework;
using System.Reflection;
using UnityEngine;

namespace HciRobot.Simulator.Tests
{
    public sealed class MotionMappingTests
    {
        private static RobotCommand Mirror(string id, string phase, float steer = 0f, float lateral = 0f)
            => new RobotCommand(0f, steer, false, null, RobotMotionMode.Continuous, lateral, id, phase);

        [TestCase(false)]
        [TestCase(true)]
        public void MirroredStep_MovesAcrossFramesAndHoldsAtFiniteBudget(bool lateral)
        {
            var robot = new GameObject("mirrored step");
            try
            {
                var body = robot.AddComponent<Rigidbody>();
                body.useGravity = false;
                var driver = robot.AddComponent<TonyPiMotionDriver>();
                var flags = BindingFlags.Instance | BindingFlags.NonPublic;
                typeof(TonyPiMotionDriver).GetMethod("Awake", flags).Invoke(driver, null);
                var tick = typeof(TonyPiMotionDriver).GetMethod("FixedUpdate", flags);
                driver.ApplyCommand(Mirror("one", "start", lateral ? 0f : 0.35f, lateral ? 0.35f : 0f));
                float displacement = 0f;
                for (int i = 0; i < 200; i++)
                {
                    if (i == 20) driver.ApplyCommand(Mirror("one", "start", lateral ? 0f : 0.35f, lateral ? 0.35f : 0f));
                    driver.ApplyCommand(Mirror("one", "heartbeat"));
                    tick.Invoke(driver, null);
                    float speed = lateral ? body.velocity.magnitude : body.angularVelocity.y * Mathf.Rad2Deg;
                    if (i == 5 || i == 25) Assert.That(speed, Is.GreaterThan(0f));
                    displacement += speed * Time.fixedDeltaTime;
                }
                Assert.That(displacement, Is.EqualTo(lateral ? 0.02f : 8f).Within(0.001f));
                Assert.That(body.velocity.sqrMagnitude + body.angularVelocity.sqrMagnitude, Is.Zero);
                Assert.That(driver.ActiveMirroredStepId, Is.EqualTo("one")); // Waiting for real ACK.
                driver.ApplyCommand(Mirror("one", "done"));
                Assert.That(driver.ActiveMirroredStepId, Is.Null);
                Assert.That(driver.LastMirroredStepStatus, Is.EqualTo("done"));
                driver.ApplyCommand(Mirror("one", "start", 0.35f));
                tick.Invoke(driver, null);
                Assert.That(body.angularVelocity.sqrMagnitude, Is.Zero); // Duplicate cannot replay.
            }
            finally { Object.DestroyImmediate(robot); }
        }

        [TestCase("done")]
        [TestCase("cancelled")]
        [TestCase("error")]
        public void MirroredStep_EarlyTerminalStopsWithoutSnapping(string phase)
        {
            var robot = new GameObject("early ACK");
            try
            {
                var body = robot.AddComponent<Rigidbody>();
                var driver = robot.AddComponent<TonyPiMotionDriver>();
                var flags = BindingFlags.Instance | BindingFlags.NonPublic;
                typeof(TonyPiMotionDriver).GetMethod("Awake", flags).Invoke(driver, null);
                var tick = typeof(TonyPiMotionDriver).GetMethod("FixedUpdate", flags);
                driver.ApplyCommand(Mirror("late", phase));
                driver.ApplyCommand(Mirror("late", "start", 0.35f));
                Assert.That(driver.ActiveMirroredStepId, Is.Null);
                driver.ApplyCommand(Mirror("old", "start", -0.35f));
                tick.Invoke(driver, null);
                Assert.That(body.angularVelocity.y, Is.LessThan(0f));
                Quaternion pose = robot.transform.rotation;
                driver.ApplyCommand(Mirror("old", phase));
                Assert.That(body.angularVelocity.sqrMagnitude, Is.Zero);
                Assert.That(robot.transform.rotation, Is.EqualTo(pose));
                Assert.That(driver.LastMirroredStepStatus, Is.EqualTo(phase));
                driver.ApplyCommand(Mirror("new", "start", 0.6f));
                driver.ApplyCommand(Mirror("old", "done"));
                Assert.That(driver.ActiveMirroredStepId, Is.EqualTo("new"));
                driver.ApplyCommand(RobotCommand.Stop);
                driver.ApplyCommand(Mirror("new", "start", 0.6f));
                Assert.That(driver.ActiveMirroredStepId, Is.Null);
                driver.ApplyCommand(Mirror("timeout", "start", 0.35f));
                typeof(TonyPiMotionDriver).GetField("stepStartedRealtime", flags).SetValue(driver, -100f);
                tick.Invoke(driver, null);
                Assert.That(driver.ActiveMirroredStepId, Is.Null);
                Assert.That(driver.LastMirroredStepStatus, Is.EqualTo("timeout"));
            }
            finally { Object.DestroyImmediate(robot); }
        }
        [TestCase(0.8f, 0.7f, RobotMotionMode.TurnRight)]
        [TestCase(0.8f, -0.7f, RobotMotionMode.TurnLeft)]
        [TestCase(0.8f, 0.2f, RobotMotionMode.Forward)]
        [TestCase(-0.8f, 0.2f, RobotMotionMode.Backward)]
        [TestCase(0.2f, 0.2f, RobotMotionMode.Stand)]
        public void DiscreteMapping_UsesTurnPriorityAndTonyPiSigns(float velocity, float steer, RobotMotionMode expected)
        {
            Assert.That(TonyPiCommandMapper.MapDiscrete(velocity, steer, 0.20f), Is.EqualTo(expected));
        }

        [Test]
        public void ContinuousMapping_ZeroesOnlyDeadzoneAxes()
        {
            RobotCommand command = TonyPiCommandMapper.ToCommand(0.15f, -0.4f, false, 0.20f, true);

            Assert.That(command.Velocity, Is.Zero);
            Assert.That(command.Steer, Is.EqualTo(-0.4f).Within(0.0001f));
            Assert.That(command.Mode, Is.EqualTo(RobotMotionMode.Continuous));
        }

        [TestCase(0.30f, 0.015f)]
        [TestCase(0.60f, 0.0375f)]
        [TestCase(0.85f, 0.075f)]
        [TestCase(-0.30f, -0.015f)]
        public void Driver_MeasuredForwardTiersMatchHardwareCalibration(float input, float expected)
        {
            var robot = new GameObject("forward tier test");
            try
            {
                robot.AddComponent<Rigidbody>().useGravity = false;
                var driver = robot.AddComponent<TonyPiMotionDriver>();
                Assert.That(driver.CalculateMeasuredForwardSpeed(input),
                    Is.EqualTo(expected).Within(0.00001f));
            }
            finally
            {
                Object.DestroyImmediate(robot);
            }
        }

        [TestCase(-0.35f, RobotMotionMode.LateralLeft)]
        [TestCase(0.35f, RobotMotionMode.LateralRight)]
        public void LateralMapping_IsExclusiveAndBelowTurnPriority(float lateral, RobotMotionMode mode)
        {
            Assert.That(TonyPiCommandMapper.MapDiscrete(0.6f, 0f, 0.2f, lateral), Is.EqualTo(mode));
            RobotCommand discrete = TonyPiCommandMapper.ToCommand(0.6f, 0f, false, 0.2f, false, lateral);
            Assert.That(discrete.Velocity, Is.Zero);
            Assert.That(discrete.Steer, Is.Zero);
            Assert.That(discrete.Lateral, Is.EqualTo(Mathf.Sign(lateral)));

            RobotCommand continuous = TonyPiCommandMapper.ToCommand(0.6f, 0f, false, 0.2f, true, lateral);
            Assert.That(continuous.Velocity, Is.Zero);
            Assert.That(continuous.Steer, Is.Zero);
            Assert.That(continuous.Lateral, Is.EqualTo(lateral));

            foreach (bool useContinuous in new[] { false, true })
            {
                RobotCommand turn = TonyPiCommandMapper.ToCommand(0.6f, 0.4f, false, 0.2f, useContinuous, lateral);
                Assert.That(turn.Lateral, Is.Zero);
                Assert.That(turn.Velocity, Is.Zero);
                Assert.That(turn.Steer, Is.GreaterThan(0f));
            }
        }

        [TestCase(false, -0.35f)]
        [TestCase(false, 0.35f)]
        [TestCase(true, -0.35f)]
        [TestCase(true, 0.35f)]
        public void Driver_MovesAlongBodyRightAndStopClearsLateral(bool continuous, float lateral)
        {
            var robot = new GameObject("lateral test");
            try
            {
                robot.transform.rotation = Quaternion.Euler(0f, 90f, 0f);
                var body = robot.AddComponent<Rigidbody>();
                body.useGravity = false;
                var driver = robot.AddComponent<TonyPiMotionDriver>();
                driver.ContinuousMotion = continuous;
                // EditMode does not schedule MonoBehaviour messages. Invoke the
                // lifecycle methods directly while exercising the real Rigidbody.
                var flags = BindingFlags.Instance | BindingFlags.NonPublic;
                typeof(TonyPiMotionDriver).GetMethod("Awake", flags).Invoke(driver, null);
                var tick = typeof(TonyPiMotionDriver).GetMethod("FixedUpdate", flags);
                driver.ApplyCommand(TonyPiCommandMapper.ToCommand(0f, 0f, false, 0.2f, continuous, lateral));
                tick.Invoke(driver, null);
                Assert.That(Vector3.Dot(body.velocity, robot.transform.right) * Mathf.Sign(lateral), Is.GreaterThan(0f));
                Assert.That(Vector3.Dot(body.velocity, robot.transform.forward), Is.EqualTo(0f).Within(0.0001f));
                Assert.That(body.angularVelocity.sqrMagnitude, Is.Zero);
                Assert.That(driver.ActualLateralOutput, Is.Not.Zero);
                driver.StopMotion();
                tick.Invoke(driver, null);
                Assert.That(body.velocity.sqrMagnitude, Is.Zero);
                Assert.That(driver.ActualLateralOutput, Is.Zero);
            }
            finally
            {
                Object.DestroyImmediate(robot);
            }
        }

        [Test]
        public void HeadActions_SelectThreePersistentPitchesWithoutChangingOnBodyTurn()
        {
            var robot = new GameObject("head pitch test");
            var camera = new GameObject("Robot Camera");
            try
            {
                camera.transform.SetParent(robot.transform, false);
                robot.AddComponent<Rigidbody>().useGravity = false;
                var driver = robot.AddComponent<TonyPiMotionDriver>();
                var flags = BindingFlags.Instance | BindingFlags.NonPublic;
                typeof(TonyPiMotionDriver).GetMethod("Awake", flags).Invoke(driver, null);

                driver.ApplyCommand(TonyPiCommandMapper.Action("head_down"));
                Assert.That(driver.HeadPitchLevel, Is.EqualTo("down"));
                Assert.That(camera.transform.localEulerAngles.x, Is.EqualTo(30f).Within(0.001f));
                driver.ApplyCommand(TonyPiCommandMapper.ToCommand(0f, 0.6f, false, 0.2f, true));
                Assert.That(camera.transform.localEulerAngles.x, Is.EqualTo(30f).Within(0.001f));
                driver.ApplyCommand(TonyPiCommandMapper.Action("head_center"));
                Assert.That(camera.transform.localEulerAngles.x, Is.EqualTo(10f).Within(0.001f));
                driver.ApplyCommand(TonyPiCommandMapper.Action("head_up"));
                Assert.That(driver.HeadPitchLevel, Is.EqualTo("up"));
                Assert.That(Mathf.DeltaAngle(camera.transform.localEulerAngles.x, -10f),
                    Is.Zero.Within(0.001f));

                driver.ApplyCommand(TonyPiCommandMapper.Action("head_up", "a1", "done"));
                Assert.That(driver.LastActionReceiptId, Is.EqualTo("a1"));
                Assert.That(driver.LastActionReceiptStatus, Is.EqualTo("done"));
            }
            finally
            {
                Object.DestroyImmediate(camera);
                Object.DestroyImmediate(robot);
            }
        }
    }
}
