using UnityEditor;
using UnityEditor.SceneManagement;
using UnityEngine;
using UnityEngine.SceneManagement;

namespace HciRobot.Simulator.Editor
{
    public static class QuickStartSceneBuilder
    {
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
            var serialized = new SerializedObject(target);
            SerializedProperty property = serialized.FindProperty(propertyName);
            if (property != null)
            {
                property.objectReferenceValue = value;
                serialized.ApplyModifiedPropertiesWithoutUndo();
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
