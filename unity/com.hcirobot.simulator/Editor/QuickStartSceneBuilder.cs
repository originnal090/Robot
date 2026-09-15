using UnityEditor;
using UnityEditor.SceneManagement;
using UnityEngine;
using UnityEngine.SceneManagement;

namespace HciRobot.Simulator.Editor
{
    public static class QuickStartSceneBuilder
    {
        private const string BindCameraMenu = "HCIRobot/Bind Selected Camera (No Target)";

        [MenuItem("HCIRobot/Build Quick Start Scene")]
        public static void BuildQuickStartScene()
        {
            Scene scene = EditorSceneManager.NewScene(NewSceneSetup.EmptyScene, NewSceneMode.Single);

            GameObject environment = new GameObject("Environment");
            CreateGround(environment.transform);
            GameObject target = CreateTarget(environment.transform);
            CreateObstacle(environment.transform, "Obstacle A", new Vector3(1.8f, 0.5f, 3.1f), new Vector3(0.7f, 1f, 0.7f));
            CreateObstacle(environment.transform, "Obstacle B", new Vector3(-1.5f, 0.35f, 5.2f), new Vector3(0.8f, 0.7f, 1.2f));
            CreateLights(environment.transform);

            GameObject robot = CreateRobot();
            CreateObserverCamera(robot.transform);
            GameObject services = new GameObject("HCIRobot Services");
            services.AddComponent<KeepRunningInBackground>();
            var status = services.AddComponent<AutonomyStatusUdpReceiver>();
            var hud = services.AddComponent<AutonomyStatusHud>();
            AssignObject(hud, "statusReceiver", status);
            AssignObject(hud, "motionDriver", robot.GetComponent<TonyPiMotionDriver>());

            var recorder = robot.AddComponent<SimulationTrialRecorder>();
            AssignObject(recorder, "robot", robot.transform);
            AssignObject(recorder, "target", target.transform);
            AssignObject(recorder, "robotBody", robot.GetComponent<Rigidbody>());
            AssignObject(recorder, "motionDriver", robot.GetComponent<TonyPiMotionDriver>());
            AssignObject(recorder, "distanceSensor", robot.GetComponentInChildren<ForwardDistanceSensor>());
            AssignObject(recorder, "statusReceiver", status);

            Selection.activeGameObject = robot;
            EditorSceneManager.MarkSceneDirty(scene);
            Debug.Log("HCIRobot quick-start scene created. Save it before entering Play mode.");
        }

        [MenuItem(BindCameraMenu)]
        public static void BindSelectedCameraWithoutTarget()
        {
            Camera camera = FindCameraInSelection();
            if (!BindExistingCamera(camera, out string error))
            {
                EditorUtility.DisplayDialog("HCIRobot camera binding", error, "OK");
                return;
            }

            Selection.activeGameObject = camera.GetComponentInParent<Rigidbody>().gameObject;
            Debug.Log(
                $"HCIRobot bound to existing camera '{camera.name}'. The current scene was preserved and no target ball was created.",
                camera);
        }

        [MenuItem(BindCameraMenu, true)]
        private static bool ValidateBindSelectedCameraWithoutTarget()
        {
            return FindCameraInSelection() != null;
        }

        /// <summary>
        /// Adds the HCIRobot protocol, video, distance and status components around an
        /// existing scene camera. The nearest parent Rigidbody is treated as the moving
        /// character root. This method never creates a target or replaces the scene.
        /// </summary>
        public static bool BindExistingCamera(Camera camera, out string error)
        {
            if (camera == null)
            {
                error = "Select the firefighter Camera (or its parent) in the Hierarchy first.";
                return false;
            }

            Rigidbody body = camera.GetComponentInParent<Rigidbody>();
            if (body == null)
            {
                error = "The selected Camera needs a Rigidbody somewhere in its parent hierarchy. Add one to the firefighter root, then try again.";
                return false;
            }

            GameObject robot = body.gameObject;
            VirtualRobotTcpServer otherTcp = FindOtherComponent<VirtualRobotTcpServer>(robot);
            if (otherTcp != null)
            {
                error = $"Another VirtualRobotTcpServer already exists on '{otherTcp.gameObject.name}'. Remove or disable that binding before binding this firefighter.";
                return false;
            }

            MjpegCameraServer otherMjpeg = FindOtherComponent<MjpegCameraServer>(camera.gameObject);
            if (otherMjpeg != null)
            {
                error = $"Another MjpegCameraServer already exists on '{otherMjpeg.gameObject.name}'. Remove or disable that binding before binding this Camera.";
                return false;
            }

            TonyPiMotionDriver driver = GetOrAddComponent<TonyPiMotionDriver>(robot, out bool driverAdded);
            AssignObject(driver, "body", body);
            AssignObject(driver, "headPitchTransform", camera.transform);
            if (driverAdded)
            {
                SetSerializedBool(driver, "continuousMotion", true);
                SetSerializedFloat(driver, "maximumTurnDegreesPerSecond", 30f);
                SetSerializedFloat(driver, "maximumForwardSpeed", 0.8f);
                SetSerializedFloat(driver, "deadzone", 0.05f);

                float centerPitch = Mathf.DeltaAngle(0f, camera.transform.localEulerAngles.x);
                SetSerializedFloat(driver, "headCenterPitchDegrees", centerPitch);
                SetSerializedFloat(driver, "headDownPitchDegrees", centerPitch + 20f);
                SetSerializedFloat(driver, "headUpPitchDegrees", centerPitch - 20f);
            }

            VirtualRobotTcpServer tcp = GetOrAddComponent<VirtualRobotTcpServer>(robot, out bool tcpAdded);
            AssignObject(tcp, "motionDriver", driver);
            if (tcpAdded)
            {
                SetSerializedBool(tcp, "continuousMotion", true);
                SetSerializedFloat(tcp, "deadzone", 0.05f);
            }

            MjpegCameraServer mjpeg = GetOrAddComponent<MjpegCameraServer>(camera.gameObject, out bool mjpegAdded);
            AssignObject(mjpeg, "sourceCamera", camera);
            if (mjpegAdded)
            {
                SetSerializedInt(mjpeg, "framesPerSecond", 25);
            }

            ForwardDistanceSensor distance = robot.GetComponentInChildren<ForwardDistanceSensor>(true);
            if (distance == null)
            {
                GameObject sensor = new GameObject("HCIRobot Forward Distance Sensor");
                Undo.RegisterCreatedObjectUndo(sensor, "Bind HCIRobot camera");
                sensor.transform.SetParent(robot.transform, true);
                sensor.transform.position = camera.transform.position;
                // Obstacle ranging follows body heading and is not tilted when the
                // camera looks down at a nearby ball.
                sensor.transform.rotation = robot.transform.rotation;
                distance = Undo.AddComponent<ForwardDistanceSensor>(sensor);
            }
            AssignObject(distance, "tcpServer", tcp);

            ConfigureSceneServices(driver);

            EditorSceneManager.MarkSceneDirty(camera.gameObject.scene);
            error = null;
            return true;
        }

        private static Camera FindCameraInSelection()
        {
            GameObject selected = Selection.activeGameObject;
            if (selected == null)
            {
                return null;
            }
            return selected.GetComponent<Camera>() ?? selected.GetComponentInChildren<Camera>(true);
        }

        private static T FindOtherComponent<T>(GameObject expectedOwner) where T : Component
        {
            T[] components = Object.FindObjectsOfType<T>(true);
            foreach (T component in components)
            {
                if (component.gameObject != expectedOwner)
                {
                    return component;
                }
            }
            return null;
        }

        private static T GetOrAddComponent<T>(GameObject owner, out bool added) where T : Component
        {
            T component = owner.GetComponent<T>();
            added = component == null;
            return component ?? Undo.AddComponent<T>(owner);
        }

        private static void ConfigureSceneServices(TonyPiMotionDriver driver)
        {
            AutonomyStatusUdpReceiver status = Object.FindObjectOfType<AutonomyStatusUdpReceiver>(true);
            AutonomyStatusHud hud = Object.FindObjectOfType<AutonomyStatusHud>(true);
            KeepRunningInBackground keepRunning = Object.FindObjectOfType<KeepRunningInBackground>(true);

            GameObject services = status != null ? status.gameObject
                : hud != null ? hud.gameObject
                : keepRunning != null ? keepRunning.gameObject
                : null;
            if (services == null)
            {
                services = new GameObject("HCIRobot Services");
                Undo.RegisterCreatedObjectUndo(services, "Bind HCIRobot camera");
            }

            if (keepRunning == null)
            {
                keepRunning = Undo.AddComponent<KeepRunningInBackground>(services);
            }
            if (status == null)
            {
                status = Undo.AddComponent<AutonomyStatusUdpReceiver>(services);
            }
            if (hud == null)
            {
                hud = Undo.AddComponent<AutonomyStatusHud>(services);
            }
            AssignObject(hud, "statusReceiver", status);
            AssignObject(hud, "motionDriver", driver);
        }

        private static GameObject CreateRobot()
        {
            GameObject robot = GameObject.CreatePrimitive(PrimitiveType.Capsule);
            robot.name = "Virtual TonyPi";
            robot.transform.position = new Vector3(0f, 0.8f, 0f);
            robot.transform.localScale = new Vector3(0.55f, 0.8f, 0.45f);
            SetMaterialColor(robot, new Color(0.14f, 0.35f, 0.65f));

            Rigidbody body = robot.AddComponent<Rigidbody>();
            body.mass = 3f;
            body.interpolation = RigidbodyInterpolation.Interpolate;
            body.constraints = RigidbodyConstraints.FreezeRotationX | RigidbodyConstraints.FreezeRotationZ;
            robot.layer = 2; // Ignore Raycast: the forward sensor must not hit the robot itself.

            TonyPiMotionDriver driver = robot.AddComponent<TonyPiMotionDriver>();
            // The Python controller's align law is proportional (steer shrinks
            // near center), matching the real TonyPi firmware response. Use the
            // driver's continuous mode so small steer commands turn slowly
            // instead of snapping to the full discrete rate. Turn rate stays
            // low enough that the align band resolves even when an unfocused
            // Unity editor throttles Play Mode to a few frames per second.
            SetSerializedBool(driver, "continuousMotion", true);
            SetSerializedFloat(driver, "maximumTurnDegreesPerSecond", 30f);
            SetSerializedFloat(driver, "maximumForwardSpeed", 0.8f);
            // The 0.20 TonyPi deadzone matches the physical firmware; a fine
            // 0.05 deadzone lets the simulator execute the controller's small
            // centering corrections instead of dropping them.
            SetSerializedFloat(driver, "deadzone", 0.05f);
            VirtualRobotTcpServer tcp = robot.AddComponent<VirtualRobotTcpServer>();
            AssignObject(tcp, "motionDriver", driver);
            // The server-side parser must pass proportional values through too.
            SetSerializedBool(tcp, "continuousMotion", true);
            SetSerializedFloat(tcp, "deadzone", 0.05f);

            GameObject sensor = new GameObject("Forward Distance Sensor");
            sensor.transform.SetParent(robot.transform, false);
            sensor.transform.localPosition = new Vector3(0f, 0.25f, 0.7f);
            sensor.transform.localRotation = Quaternion.identity;
            ForwardDistanceSensor distance = sensor.AddComponent<ForwardDistanceSensor>();
            AssignObject(distance, "tcpServer", tcp);

            GameObject cameraObject = new GameObject("Robot Camera");
            cameraObject.transform.SetParent(robot.transform, false);
            // World height ~0.5 m and a downward pitch match the real TonyPi
            // head camera: a ground ball stays in frame until arrival range.
            // (Robot pivot sits at its centre, y=0.8.)
            cameraObject.transform.localPosition = new Vector3(0f, -0.3f, 0.35f);
            cameraObject.transform.localRotation = Quaternion.Euler(10f, 0f, 0f);
            AssignObject(driver, "headPitchTransform", cameraObject.transform);
            Camera camera = cameraObject.AddComponent<Camera>();
            camera.fieldOfView = 62f;
            camera.nearClipPlane = 0.05f;
            camera.farClipPlane = 100f;
            // Full-frame: this camera feeds the MJPEG stream, and Camera.rect
            // also applies when rendering into the capture RenderTexture.
            camera.depth = 0f;
            cameraObject.AddComponent<AudioListener>();
            MjpegCameraServer mjpeg = cameraObject.AddComponent<MjpegCameraServer>();
            AssignObject(mjpeg, "sourceCamera", camera);
            // The discrete search turn sweeps fast; 25 fps gives the Python
            // detector enough consecutive frames to confirm the ball.
            SetSerializedInt(mjpeg, "framesPerSecond", 25);

            return robot;
        }

        private static void CreateObserverCamera(Transform robot)
        {
            GameObject observerObject = new GameObject("Observer Camera");
            observerObject.transform.position = new Vector3(0f, 3.2f, -3.4f);
            Camera observer = observerObject.AddComponent<Camera>();
            // Third-person chase view as a corner picture-in-picture on top of
            // the robot camera in the Game view; the robot camera stays full.
            observer.depth = 1f;
            observer.rect = new Rect(0.63f, 0.63f, 0.34f, 0.34f);
            observer.farClipPlane = 200f;
            observerObject.tag = "MainCamera";
            var rig = observerObject.AddComponent<ObserverCameraRig>();
            AssignObject(rig, "target", robot);
        }

        private static void CreateGround(Transform parent)
        {
            GameObject ground = GameObject.CreatePrimitive(PrimitiveType.Plane);
            ground.name = "Ground";
            ground.transform.SetParent(parent);
            ground.transform.position = Vector3.zero;
            ground.transform.localScale = new Vector3(2.5f, 1f, 2.5f);
            SetMaterialColor(ground, new Color(0.35f, 0.37f, 0.39f));
        }

        private static GameObject CreateTarget(Transform parent)
        {
            GameObject target = GameObject.CreatePrimitive(PrimitiveType.Sphere);
            target.name = "Red Ball Target";
            target.transform.SetParent(parent);
            target.transform.position = new Vector3(0.8f, 0.35f, 6.0f);
            target.transform.localScale = Vector3.one * 0.7f;
            target.layer = 2; // Ignore Raycast: the target is visual ground truth, not an obstacle.
            // Magenta-red maps near LAB (139, 210, 148), inside config.toml's tuned bounds.
            SetMaterialColor(target, new Color(1f, 0.04f, 0.39f));
            return target;
        }

        private static void CreateObstacle(Transform parent, string name, Vector3 position, Vector3 scale)
        {
            GameObject obstacle = GameObject.CreatePrimitive(PrimitiveType.Cube);
            obstacle.name = name;
            obstacle.transform.SetParent(parent);
            obstacle.transform.position = position;
            obstacle.transform.localScale = scale;
            SetMaterialColor(obstacle, new Color(0.85f, 0.65f, 0.16f));
        }

        private static void CreateLights(Transform parent)
        {
            GameObject sun = new GameObject("Directional Light");
            sun.transform.SetParent(parent);
            sun.transform.rotation = Quaternion.Euler(45f, -30f, 0f);
            Light light = sun.AddComponent<Light>();
            light.type = LightType.Directional;
            light.intensity = 1.1f;
            light.shadows = LightShadows.Soft;

            RenderSettings.ambientMode = UnityEngine.Rendering.AmbientMode.Trilight;
            RenderSettings.ambientSkyColor = new Color(0.55f, 0.60f, 0.68f);
            RenderSettings.ambientEquatorColor = new Color(0.30f, 0.32f, 0.35f);
            RenderSettings.ambientGroundColor = new Color(0.12f, 0.12f, 0.12f);
        }

        private static void SetMaterialColor(GameObject target, Color color)
        {
            Renderer renderer = target.GetComponent<Renderer>();
            if (renderer == null)
            {
                return;
            }

            Shader shader = Shader.Find("Standard") ?? Shader.Find("Universal Render Pipeline/Lit");
            if (shader == null)
            {
                return;
            }

            Material material = new Material(shader)
            {
                name = target.name + " Material",
                color = color
            };
            renderer.sharedMaterial = material;
        }

        private static void AssignObject(Object target, string propertyName, Object value)
        {
            Undo.RecordObject(target, "Configure HCIRobot component");
            var serialized = new SerializedObject(target);
            SerializedProperty property = serialized.FindProperty(propertyName);
            if (property != null)
            {
                property.objectReferenceValue = value;
                serialized.ApplyModifiedProperties();
            }
        }

        private static void SetSerializedInt(Object target, string propertyName, int value)
        {
            var serialized = new SerializedObject(target);
            SerializedProperty property = serialized.FindProperty(propertyName);
            if (property != null && property.propertyType == SerializedPropertyType.Integer)
            {
                property.intValue = value;
                serialized.ApplyModifiedPropertiesWithoutUndo();
            }
        }

        private static void SetSerializedFloat(Object target, string propertyName, float value)
        {
            var serialized = new SerializedObject(target);
            SerializedProperty property = serialized.FindProperty(propertyName);
            if (property != null && property.propertyType == SerializedPropertyType.Float)
            {
                property.floatValue = value;
                serialized.ApplyModifiedPropertiesWithoutUndo();
            }
        }

        private static void SetSerializedBool(Object target, string propertyName, bool value)
        {
            var serialized = new SerializedObject(target);
            SerializedProperty property = serialized.FindProperty(propertyName);
            if (property != null && property.propertyType == SerializedPropertyType.Boolean)
            {
                property.boolValue = value;
                serialized.ApplyModifiedPropertiesWithoutUndo();
            }
        }
    }
}
