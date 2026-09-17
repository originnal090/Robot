using UnityEngine;
using UnityEditor;
using UnityEngine.Rendering;

public static class DisableProbes
{
    [MenuItem("Tools/Lighting/Disable Light And Reflection Probes In Selection")]
    private static void DisableAllProbes()
    {
        int count = 0;

        foreach (GameObject selectedObject in Selection.gameObjects)
        {
            MeshRenderer[] renderers =
                selectedObject.GetComponentsInChildren<MeshRenderer>(true);

            foreach (MeshRenderer renderer in renderers)
            {
                Undo.RecordObject(renderer, "Disable Light And Reflection Probes");

                // 关闭 Light Probe
                renderer.lightProbeUsage = LightProbeUsage.Off;

                // 关闭 Reflection Probe
                renderer.reflectionProbeUsage = ReflectionProbeUsage.Off;

                EditorUtility.SetDirty(renderer);
                count++;
            }
        }

        Debug.Log(
            $"Disabled Light Probes and Reflection Probes on {count} MeshRenderers."
        );
    }

    [MenuItem(
        "Tools/Lighting/Disable Light And Reflection Probes In Selection",
        true)]
    private static bool ValidateDisableAllProbes()
    {
        return Selection.gameObjects.Length > 0;
    }
}