using System.IO;
using UnityEditor;
using UnityEditor.SceneManagement;
using UnityEngine;

namespace HciRobot.Simulator.Editor
{
    /// <summary>
    /// Entry points for the one-click Python demo (``tools/run_unity_demo.py``)
    /// and the "HCIRobot" menu. LaunchDemo opens the saved Quick Start scene —
    /// building it from the current package code when missing — and enters Play
    /// Mode. RebuildAndPlay discards the saved scene first, for when the package
    /// gained components the saved scene predates.
    /// </summary>
    public static class DemoLauncher
    {
        private const string ScenePath = "Assets/Scenes/HCIRobotQuickStart.unity";

        [MenuItem("HCIRobot/Launch Demo Scene")]
        public static void LaunchDemo()
        {
            // The editor throttles unfocused Play Mode to ~1 fps otherwise,
            // which shows up as ~1.5 s video latency in Python.
            PlayerSettings.runInBackground = true;
            OpenOrBuildScene();
            EditorApplication.isPlaying = true;
        }

        [MenuItem("HCIRobot/Rebuild Demo Scene and Play")]
        public static void RebuildAndPlay()
        {
            PlayerSettings.runInBackground = true;
            if (File.Exists(ScenePath))
            {
                AssetDatabase.DeleteAsset(ScenePath);
            }
            BuildScene();
            EditorApplication.isPlaying = true;
        }

        private static void OpenOrBuildScene()
        {
            if (File.Exists(ScenePath))
            {
                EditorSceneManager.OpenScene(ScenePath, OpenSceneMode.Single);
                return;
            }
            BuildScene();
        }

        private static void BuildScene()
        {
            QuickStartSceneBuilder.BuildQuickStartScene();
            const string scenesDirectory = "Assets/Scenes";
            if (!Directory.Exists(scenesDirectory))
            {
                Directory.CreateDirectory(scenesDirectory);
            }
            EditorSceneManager.SaveScene(EditorSceneManager.GetActiveScene(), ScenePath);
            AssetDatabase.SaveAssets();
        }
    }
}
