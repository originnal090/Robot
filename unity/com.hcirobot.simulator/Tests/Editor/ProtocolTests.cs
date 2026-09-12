using System.Collections.Generic;
using System.Text;
using NUnit.Framework;

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
    }
}
