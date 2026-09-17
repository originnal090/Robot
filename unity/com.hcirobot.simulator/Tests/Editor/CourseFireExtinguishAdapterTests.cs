using System.Reflection;
using NUnit.Framework;
using UnityEngine;

namespace HciRobot.Simulator.Tests
{
    public sealed class CompatibleFireControlForTests : MonoBehaviour
    {
        public bool legacyInstantExtinguishOnAction;
        public float extinguishDistance;
        public int triggerCount;

        private void OnActionTriggered()
        {
            if (legacyInstantExtinguishOnAction)
                triggerCount++;
        }
    }

    public sealed class CourseFireExtinguishAdapterTests
    {
        [TestCase("right_grip")]
        [TestCase("outfire")]
        public void ExtinguishAction_UsesScaledRangeAndCourseTrigger(string action)
        {
            var robot = new GameObject("fire adapter robot");
            var fireObject = new GameObject("course fire control");
            try
            {
                robot.AddComponent<Rigidbody>().useGravity = false;
                var driver = robot.AddComponent<TonyPiMotionDriver>();
                driver.MovementSpeedMultiplier = 21.5f;
                var adapter = robot.AddComponent<CourseFireExtinguishAdapter>();
                var fireControl = fireObject.AddComponent<CompatibleFireControlForTests>();
                adapter.FireControl = fireControl;
                adapter.ExtinguishRangeMetres = 1.2f;

                InvokeLifecycle(adapter, "OnEnable");
                driver.ApplyCommand(TonyPiCommandMapper.Action(action));

                Assert.That(adapter.LastRequestDispatched, Is.True);
                Assert.That(fireControl.legacyInstantExtinguishOnAction, Is.True);
                Assert.That(fireControl.extinguishDistance, Is.EqualTo(25.8f).Within(0.0001f));
                Assert.That(fireControl.triggerCount, Is.EqualTo(1));
            }
            finally
            {
                Object.DestroyImmediate(fireObject);
                Object.DestroyImmediate(robot);
            }
        }

        [Test]
        public void UnrelatedAction_DoesNotTouchCourseFireControl()
        {
            var robot = new GameObject("non-fire action robot");
            var fireObject = new GameObject("course fire control");
            try
            {
                robot.AddComponent<Rigidbody>().useGravity = false;
                var driver = robot.AddComponent<TonyPiMotionDriver>();
                var adapter = robot.AddComponent<CourseFireExtinguishAdapter>();
                var fireControl = fireObject.AddComponent<CompatibleFireControlForTests>();
                adapter.FireControl = fireControl;

                InvokeLifecycle(adapter, "OnEnable");
                driver.ApplyCommand(TonyPiCommandMapper.Action("head_down"));

                Assert.That(adapter.LastRequestDispatched, Is.False);
                Assert.That(fireControl.legacyInstantExtinguishOnAction, Is.False);
                Assert.That(fireControl.triggerCount, Is.Zero);
            }
            finally
            {
                Object.DestroyImmediate(fireObject);
                Object.DestroyImmediate(robot);
            }
        }

        [Test]
        public void PublicDispatch_ResolvesDriverEvenBeforeLifecycleCallbacks()
        {
            var robot = new GameObject("pre-lifecycle fire adapter robot");
            var fireObject = new GameObject("course fire control");
            try
            {
                robot.AddComponent<Rigidbody>().useGravity = false;
                var driver = robot.AddComponent<TonyPiMotionDriver>();
                driver.MovementSpeedMultiplier = 21.5f;
                var adapter = robot.AddComponent<CourseFireExtinguishAdapter>();
                var fireControl = fireObject.AddComponent<CompatibleFireControlForTests>();
                adapter.FireControl = fireControl;
                typeof(CourseFireExtinguishAdapter).GetField(
                    "motionDriver", BindingFlags.Instance | BindingFlags.NonPublic).SetValue(adapter, null);

                Assert.That(adapter.TryDispatch("right_grip"), Is.True);
                Assert.That(fireControl.extinguishDistance, Is.EqualTo(25.8f).Within(0.0001f));
            }
            finally
            {
                Object.DestroyImmediate(fireObject);
                Object.DestroyImmediate(robot);
            }
        }

        [Test]
        public void DriverAwake_AutoAttachesCourseFireAdapter()
        {
            var robot = new GameObject("auto fire adapter robot");
            try
            {
                robot.AddComponent<Rigidbody>().useGravity = false;
                var driver = robot.AddComponent<TonyPiMotionDriver>();

                typeof(TonyPiMotionDriver).GetMethod(
                    "Awake", BindingFlags.Instance | BindingFlags.NonPublic).Invoke(driver, null);

                Assert.That(robot.GetComponents<CourseFireExtinguishAdapter>(), Has.Length.EqualTo(1));
            }
            finally
            {
                Object.DestroyImmediate(robot);
            }
        }

        private static void InvokeLifecycle(MonoBehaviour target, string method)
        {
            target.GetType().GetMethod(
                method, BindingFlags.Instance | BindingFlags.NonPublic).Invoke(target, null);
        }
    }
}
