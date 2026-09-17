#if UNITY_EDITOR
using UnityEditor;
using UnityEditor.SceneManagement;
using UnityEngine;

public static class RobotVideoSetupTool
{
    [MenuItem("Tools/TonyPi Video/Create Video Receiver")]
    private static void CreateVideoReceiver()
    {
        RobotVideoReceiver existing = Object.FindFirstObjectByType<RobotVideoReceiver>();
        if (existing != null)
        {
            Selection.activeGameObject = existing.gameObject;
            Debug.Log("[ROBOT VIDEO] A receiver already exists in the active scene.");
            return;
        }

        GameObject host = new GameObject("RobotVideoReceiver");
        Undo.RegisterCreatedObjectUndo(host, "Create TonyPi Video Receiver");
        host.AddComponent<RobotVideoReceiver>();
        Selection.activeGameObject = host;
        EditorSceneManager.MarkSceneDirty(host.scene);
        Debug.Log("[ROBOT VIDEO] Created an independent TonyPi video receiver.");
    }
}
#endif
