using NUnit.Framework;
using UnityEngine;

namespace HciRobot.Simulator.Tests
{
    public sealed class AutonomyStatusTests
    {
        [Test]
        public void StatusDto_RequiresSchemaSessionSequenceAndState()
        {
            const string json = "{\"type\":\"autonomy_status\",\"schema_version\":1,\"session_id\":\"12345678-1234-5678-1234-567812345678\",\"seq\":4,\"sent_at_ms\":1700000000123,\"control_state\":\"APPROACHING\",\"output\":{\"v\":0.2,\"steer\":0,\"grab\":false,\"source\":\"autonomy\"}}";
            AutonomyStatusMessage message = JsonUtility.FromJson<AutonomyStatusMessage>(json);

            Assert.That(message.IsValid(AutonomyStatusUdpReceiver.DefaultSchemaVersion), Is.True);
            Assert.That(message.session_id, Is.EqualTo("12345678-1234-5678-1234-567812345678"));
            Assert.That(message.seq, Is.EqualTo(4));
        }

        [Test]
        public void SequenceFilter_IsLatestOnlyWithinSessionAndResetsOnNewSession()
        {
            var filter = new SessionSequenceFilter();

            Assert.That(filter.TryAccept("a", 2), Is.True);
            Assert.That(filter.TryAccept("a", 2), Is.False);
            Assert.That(filter.TryAccept("a", 1), Is.False);
            Assert.That(filter.TryAccept("a", 3), Is.True);
            Assert.That(filter.TryAccept("b", 0), Is.False);
            Assert.That(filter.TryAccept("b", 1), Is.True);
            Assert.That(filter.TryAccept("a", 4), Is.False);
            Assert.That(filter.Session, Is.EqualTo("b"));
            Assert.That(filter.Sequence, Is.EqualTo(1));
        }

        [Test]
        public void StatusDto_RejectsWrongSchemaOrMissingIdentity()
        {
            var message = new AutonomyStatusMessage
            {
                type = "wrong",
                schema_version = 2,
                session_id = "",
                seq = -1,
                control_state = "IDLE"
            };

            Assert.That(message.IsValid(AutonomyStatusUdpReceiver.DefaultSchemaVersion), Is.False);
        }
    }
}
