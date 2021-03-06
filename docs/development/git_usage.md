# Development Workflow

### Short Lesson
The majority of teams using git have a work flow that looks similar to the four well known work flows:

* [Git Flow](https://datasift.github.io/gitflow/IntroducingGitFlow.html)
* [GitHub Flow](https://guides.github.com/introduction/flow/)
* [GitLab Flow](https://about.gitlab.com/blog/2014/09/29/gitlab-flow/)
* [Trunk Based Development](https://trunkbaseddevelopment.com/)

## Git Development - the Shaken Fist Way

The Shaken Fist developers have chosen **Trunk Based Development.**

## Branch Types
1. `master` branch
    - This is the development trunk.
    - All `feature` branches are branched from `master` and merged to `master`.
2. `feature` branches
    - Short-lived, generally a few days.
    - Normally only one developer.
    - When presented to the team, it is expected to pass the linter and unit tests.
    - It is normal that other team members suggest changes/improvements before merging.
3. `release-vX.X` branches
    - Only created when a release requires patches (hot-fixes).
    - Commits to this branch are cherry-picks from `master`.
    - It is not expected that many commits are made to this branch.
    - If many commits are required to a release branch then this indicates the need for another release.

!!! attention
    One day the project might desire a `develop` branch to ensure that `master` is always production ready. This can be useful when adding and maturing multiple inter-dependant features. At this stage, it is not required and would lead to more complexity. At this of project maturity, that complexity would be extra effort with the possibility of errors without a significant return.

## Process

### Feature branches
* Feature/Bug branches have a prefix consisting of the GitHub issue number - no need for the word bug or issue.
* The feature branch developer should squash commits to remove WIP commits before creating a Pull Request.
* It is preferably that each remaining commit passes testing/CI.

### Merging
* Commits are **not** squashed when merged to `master`
* Not squashing commits maintains history of multiple issues being solved.
* Pull Request related commits remain grouped and can be understood as a single merge

### Release branch
* Only **necessary** bug fixes are cherry-picked from `master` to an existing `release-vX.X` branch.

## Too many cherry-picked commits to a Release branch
If a large number of commits appear desirable on a `release` branch, it is probably an indication that another minor release would be a better idea.

If another release is not desired because `master` contains unstable features then either CI needs improving or that feature requires more work and should not be in `master`.



