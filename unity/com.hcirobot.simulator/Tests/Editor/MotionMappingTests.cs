using NUnit.Framework;

namespace HciRobot.Simulator.Tests
{
    public sealed class MotionMappingTests
    {
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
    }
}
