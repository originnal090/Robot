using HciRobot.Simulator.Editor;
using NUnit.Framework;
using UnityEditor;
using UnityEditor.SceneManagement;
using UnityEngine;

namespace HciRobot.Simulator.Tests
{
    public sealed class ExistingCameraBindingTests
    {
        [Test]
        public void BindExistingCamera_PreservesSceneAndCreatesNoTarget()
        {
            EditorSceneManager.NewScene(NewSceneSetup.EmptyScene, NewSceneMode.Single);
            var landmark = new GameObject("Existing Fire Station");
            var firefighter = new GameObject("Firefighter");
            Rigidbody body = firefighter.AddComponent<Rigidbody>();
            var cameraObject = new GameObject("Firefighter Camera");
            cameraObject.transform.SetParent(firefighter.transform, false);
            cameraObject.transform.localPosition = new Vector3(0f, 1.6f, 0.1f);
            cameraObject.transform.localRotation = Quaternion.Euler(7f, 0f, 0f);
            Camera camera = cameraObject.AddComponent<Camera>();

            Assert.That(QuickStartSceneBuilder.BindExistingCamera(camera, out string error), Is.True, error);
            Assert.That(GameObject.Find("Existing Fire Station"), Is.SameAs(landmark));
            Assert.That(GameObject.Find("Red Ball Target"), Is.Null);
            Assert.That(firefighter.GetComponent<TonyPiMotionDriver>(), Is.Not.Null);
            Assert.That(firefighter.GetComponent<VirtualRobotTcpServer>(), Is.Not.Null);
            Assert.That(cameraObject.GetComponent<MjpegCameraServer>(), Is.Not.Null);
            Assert.That(firefighter.GetComponentInChildren<ForwardDistanceSensor>(), Is.Not.Null);
            Assert.That(Object.FindObjectOfType<AutonomyStatusUdpReceiver>(), Is.Not.Null);
            Assert.That(Object.FindObjectOfType<AutonomyStatusHud>(), Is.Not.Null);
            Assert.That(Object.FindObjectOfType<SimulationTrialRecorder>(), Is.Null);

            int componentCount = firefighter.GetComponentsInChildren<Component>(true).Length;
            Assert.That(QuickStartSceneBuilder.BindExistingCamera(camera, out error), Is.True, error);
            Assert.That(firefighter.GetComponentsInChildren<Component>(true).Length, Is.EqualTo(componentCount));

            var driver = new SerializedObject(firefighter.GetComponent<TonyPiMotionDriver>());
            Assert.That(driver.FindProperty("headPitchTransform").objectReferenceValue, Is.SameAs(camera.transform));
            Assert.That(driver.FindProperty("headCenterPitchDegrees").floatValue, Is.EqualTo(7f).Within(0.001f));
            Assert.That(driver.FindProperty("headDownPitchDegrees").floatValue, Is.EqualTo(27f).Within(0.001f));
            Assert.That(driver.FindProperty("headUpPitchDegrees").floatValue, Is.EqualTo(-13f).Within(0.001f));
            Assert.That(body, Is.Not.Null);
        }
    }
}
